"""Generátor osobní wiki o kanálech pro Obsidian.

Zásady:
- údaje ze serveru (stav, zdroj, program, vlastník) se při každém generování obnoví,
- všechno pod značkou NOTES_MARK patří uživateli a nikdy se nepřepíše,
- v hlavičce stránky patří uživateli klíče `wikidata` a `wikidata_overeno`,
- stránky kanálů, které z výběru zmizí, se nesmažou, jen dostanou upozornění.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from . import catalog, db, webstream, wikidata
from .checker import base_id, is_allowed, is_restream
from .config import Config
from .m3u import clean_name
from .netinfo import lan_addresses

log = logging.getLogger("tvserver.wiki")

NOTES_HEADING = "## Moje poznámky"
NOTES_MARK = "<!-- Pod tímto řádkem jsou vaše poznámky. Generátor je nikdy nepřepíše. -->"
GONE_MARK = "> [!warning] Kanál už není ve výběru Doporučené ani v mém seznamu"
CHANNELS, SOURCES, INDEXES = "Kanály", "Zdroje", "Rozcestníky"

COUNTRY_CS = {
    "CZ": "Česko", "SK": "Slovensko", "DE": "Německo", "AT": "Rakousko", "CH": "Švýcarsko", "FR": "Francie",
    "BE": "Belgie", "NL": "Nizozemsko", "LU": "Lucembursko", "UK": "Spojené království", "GB": "Spojené království",
    "IE": "Irsko", "US": "Spojené státy", "CA": "Kanada", "AU": "Austrálie", "NZ": "Nový Zéland", "PL": "Polsko",
    "HU": "Maďarsko", "IT": "Itálie", "ES": "Španělsko", "PT": "Portugalsko", "QA": "Katar", "JP": "Japonsko",
    "KR": "Jižní Korea", "SG": "Singapur", "IN": "Indie", "IL": "Izrael", "RU": "Rusko", "CN": "Čína",
    "TR": "Turecko", "MT": "Malta", "SE": "Švédsko", "DK": "Dánsko", "NO": "Norsko", "FI": "Finsko",
    "PR": "Portoriko", "HK": "Hongkong", "ZA": "Jihoafrická republika", "AE": "Spojené arabské emiráty",
}
LANG_CS = {
    "eng": "angličtina", "ces": "čeština", "slk": "slovenština", "deu": "němčina", "fra": "francouzština",
    "spa": "španělština", "ita": "italština", "pol": "polština", "rus": "ruština", "ara": "arabština",
    "jpn": "japonština", "kor": "korejština", "hun": "maďarština", "zho": "čínština", "por": "portugalština",
    "nld": "nizozemština", "hin": "hindština", "tur": "turečtina", "ukr": "ukrajinština", "ltz": "lucemburština",
    "heb": "hebrejština", "swe": "švédština", "dan": "dánština", "nor": "norština",
}


# --- platformy a zdroje ------------------------------------------------------------------

def platforms(url: str, cfg: Config) -> list[str]:
    """Na čem stream běží, podle názvu serveru. Vrací názvy stránek ve složce Zdroje."""
    host = (urlparse(url).hostname or "").lower()
    if not is_allowed(url, cfg):
        return ["Blokované zdroje"]
    if is_restream(url, cfg):
        return ["Restreamy"]
    out = []
    if webstream.is_web(url):
        return ["YouTube a Twitch"]
    if host == "jmp2.uk" or host.endswith("pluto.tv"):
        out.append("Pluto TV")
    if "samsung" in host:
        out.append("Samsung TV Plus")
    if host.endswith("amagi.tv"):
        out.append("Amagi")
    if host.endswith("wurl.com") or host.endswith("wurl.tv"):
        out.append("Wurl")
    if host.endswith("tubi.video"):
        out.append("Tubi")
    if any(x in host for x in ("akamaized.net", "akamaihd.net", "edgesuite.net", "cloudfront.net", "amazonaws.com",
                               "uplynk.com", "streamlock.net", "livepush.io", "broadpeak", "cdn77", "ard-mcdn.de")):
        out.append("CDN")
    return out or ["Vlastní server stanice"]


SOURCE_PAGES: dict[str, str] = {'Pluto TV': 'Pluto TV: veřejně dostupné streamy poskytovatele. Dostupnost závisí na regionu a podmínkách služby.', 'Samsung TV Plus': 'FAST kanály Samsung TV Plus. Seznamy a program spravuje jejich poskytovatel.', 'Amagi': 'Distribuční platforma a CDN pro televizní a FAST kanály.', 'Wurl': 'Distribuční platforma pro internetové televizní kanály.', 'Tubi': 'Streamovací služba Tubi. Dostupnost závisí na konkrétním zdroji a regionu.', 'CDN': 'Distribuční síť vysílatele. Samotná doména neurčuje vlastníka ani oprávnění k obsahu.', 'Vlastní server stanice': 'Adresa uvedená uživatelem nebo zdrojovým katalogem.', 'YouTube a Twitch': 'Živé vysílání se řeší přes yt-dlp. Používejte vlastní nebo oprávněně dostupné zdroje.', 'Restreamy': 'Přeposílané streamy podle nastavení restream_hosts. Dostupnost se ověřuje při přidání.'}


# --- pomocné funkce -----------------------------------------------------------------------

def owner_name(chain: list[dict], owners_iptv: list[str]) -> str:
    """Nejvyšší vlastník, který není stát. U France 24 je to France Médias Monde, ne Francie."""
    names = [c for c in chain if not any(w in (c["types"] + " |") for w in wikidata.COUNTRY_WORDS)]
    if names:
        return names[-1]["name"]
    if chain:
        return chain[-1]["name"]
    return owners_iptv[0] if owners_iptv else ""


def safe_name(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", name)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return s or "Bez názvu"


def q(v) -> str:
    """Hodnota pro hlavičku YAML. JSON je platné YAML a nic nerozbije."""
    return json.dumps(v, ensure_ascii=False)


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "nikdy"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%-d. %-m. %Y %H:%M")
    except ValueError:
        return iso


def read_existing(path: Path) -> tuple[str, dict]:
    """Vrátí uživatelské poznámky a uživatelské klíče z hlavičky existující stránky."""
    if not path.exists():
        return "", {}
    text = path.read_text(encoding="utf-8")
    notes = text.split(NOTES_MARK, 1)[1].lstrip("\n") if NOTES_MARK in text else ""
    keys = {}
    if text.startswith("---"):
        head = text.split("---", 2)[1]
        for key in ("wikidata", "wikidata_overeno", "vlastnictvi_rucne"):
            m = re.search(rf"^{key}:\s*(.+)$", head, re.M)
            if m:
                raw = m.group(1).strip()
                try:
                    keys[key] = json.loads(raw)
                except json.JSONDecodeError:
                    keys[key] = raw.strip('"\'')
    return notes, keys


def notes_block(notes: str) -> str:
    return f"{NOTES_HEADING}\n\n{NOTES_MARK}\n{notes if notes.strip() else ''}"


_DATE = re.compile(r'^aktualizovano: ".*"$', re.M)


def write_if_changed(path: Path, text: str) -> bool:
    """Zapíše jen změněný obsah. Samotné nové datum v hlavičce za změnu nepočítá."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and _DATE.sub("", path.read_text(encoding="utf-8")) == _DATE.sub("", text):
        return False
    path.write_text(text, encoding="utf-8")
    return True


# --- sběr dat -----------------------------------------------------------------------------

async def load_iptv_channels(cfg: Config) -> dict[str, dict]:
    path = cfg.database.parent / "wiki_cache" / "iptv_channels.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400:
        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
                r = await c.get(f"{cfg.catalog_api}/channels.json")
                r.raise_for_status()
                path.write_bytes(r.content)
        except httpx.HTTPError as e:
            log.warning("Údaje o kanálech z iptv-org se nepodařilo stáhnout: %s", e)
            if not path.exists():
                return {}
    return {c["id"].lower(): c for c in json.loads(path.read_text(encoding="utf-8"))}


def collect(con: sqlite3.Connection, cfg: Config, iptv: dict[str, dict]) -> dict[str, dict]:
    """Kanály z mého seznamu a z výběru Doporučené, seskupené po stanicích (bez jazykové verze)."""
    names = catalog.names(con)
    epg_rows = {r["channel_id"]: r for r in con.execute("SELECT * FROM epg_status")}
    out: dict[str, dict] = {}

    def entry(key: str) -> dict:
        return out.setdefault(key, {"key": key, "versions": [], "in_list": False})

    def lang_names(codes: str) -> list[str]:
        return [LANG_CS.get(c, names["languages"].get(c, c)) for c in codes.split(",") if c]

    for r in db.all_channels(con):
        key = base_id(r["tvg_id"]) if r["tvg_id"] else f"url:{r['url']}"
        cat = catalog.lookup(con, r["url"])
        e = epg_rows.get(r["id"])
        it = entry(key)
        it["in_list"] = True
        it["versions"].append({
            "name": clean_name(r["name"]), "tvg_id": r["tvg_id"], "group": r["grp"], "url": r["url"],
            "in_list": True, "channel_id": r["id"], "enabled": bool(r["enabled"]), "status": r["status"],
            "last_ok": r["last_ok"], "favorite": bool(r["favorite"]), "logo": r["logo"],
            "country": cat["country"] if cat else "", "languages": lang_names(cat["languages"]) if cat else [],
            "categories": cat["categories"] if cat else "", "has_epg": bool(e and e["count"]),
            "epg_source": e["source"] if e else "", "epg_count": e["count"] if e else 0,
        })
    for r in con.execute("SELECT * FROM catalog WHERE source = 'Doporučené'"):
        key = base_id(r["tvg_id"]) if r["tvg_id"] else f"url:{r['url']}"
        it = entry(key)
        if any(v["url"] == r["url"] for v in it["versions"]):
            continue
        it["versions"].append({
            "name": r["name"], "tvg_id": r["tvg_id"], "group": r["grp"], "url": r["url"], "in_list": False,
            "channel_id": None, "enabled": False, "status": "", "last_ok": None, "favorite": False,
            "logo": r["logo"], "country": r["country"], "languages": lang_names(r["languages"]),
            "categories": r["categories"], "has_epg": bool(r["has_epg"]), "epg_source": "", "epg_count": 0,
        })

    for key, it in out.items():
        meta = iptv.get(key) or {}
        v0 = next((v for v in it["versions"] if v["in_list"]), it["versions"][0])
        it["meta"] = meta
        # Jedna verze: její vlastní název (má diakritiku a upřesnění, např. „ABC News Australia“).
        # Více jazykových verzí: společný název stanice z iptv-org (např. „France 24“).
        it["title"] = meta["name"] if meta.get("name") and len(it["versions"]) > 1 else v0["name"]
        it["logo"] = next((v["logo"] for v in it["versions"] if v["logo"]), "")
        it["country"] = meta.get("country") or v0["country"]
        it["languages"] = sorted({lang for v in it["versions"] for lang in v["languages"]})
        cats = meta.get("categories") or [c for c in v0["categories"].split(",") if c]
        it["categories"] = [catalog.CATEGORY_CS.get(c, names["categories"].get(c, c)) for c in cats]
        it["groups"] = sorted({v["group"] for v in it["versions"] if v["group"]})
        it["restream"] = all(is_restream(v["url"], cfg) or not is_allowed(v["url"], cfg) for v in it["versions"])
        it["platforms"] = sorted({p for v in it["versions"] for p in platforms(v["url"], cfg)})
        it["has_epg"] = any(v["has_epg"] for v in it["versions"])
        # další jazykové verze téže stanice v katalogu (bez restreamů)
        have = {v["tvg_id"] for v in it["versions"]}
        others = con.execute(
            "SELECT tvg_id, name, MIN(restream) AS rs FROM catalog WHERE source = 'iptv-org' AND restream = 0 "
            "AND (lower(tvg_id) = ? OR lower(tvg_id) LIKE ?) GROUP BY tvg_id ORDER BY name",
            (key, key + "@%")).fetchall()
        it["siblings"] = [o["name"] for o in others if o["tvg_id"] not in have][:12]
    return out


# --- stránky ------------------------------------------------------------------------------

class Wiki:
    def __init__(self, con: sqlite3.Connection, cfg: Config):
        self.con, self.cfg = con, cfg
        self.root = cfg.wiki_dir
        self.prefix = cfg.obsidian_prefix.strip("/")
        self.lan = lan_addresses(cfg.port).get("ip_url") or f"http://127.0.0.1:{cfg.port}"
        self.today = date.today().isoformat()

    def link(self, folder: str | None, page: str, label: str | None = None, heading: str | None = None,
             in_table: bool = False) -> str:
        path = f"{self.prefix}/{folder}/{page}" if folder else f"{self.prefix}/{page}"
        if heading:
            path += f"#{heading}"
        sep = "\\|" if in_table else "|"   # v tabulce by „|“ rozdělil sloupec
        return f"[[{path}{sep}{label or page}]]"

    def obsidian_url(self, folder: str, page: str) -> str:
        return (f"obsidian://open?vault={quote(self.cfg.obsidian_vault)}"
                f"&file={quote(f'{self.prefix}/{folder}/{page}')}")

    # -- kanál --
    def channel_page(self, it: dict, page: str, wd: dict, user: dict) -> str:
        meta = it["meta"]
        # Údaje z Wikidat se berou jako fakta jen při jisté nebo ručně potvrzené shodě.
        # U shody k ověření zůstane jen návrh v rámečku, aby se nepřebraly údaje cizí stanice.
        suggestion = wd if wd.get("match") == "k ověření" and not user.get("wikidata_overeno") else None
        if suggestion:
            wd = {"match": "k ověření", "qid": wd.get("qid")}
        owners_iptv = meta.get("owners") or []
        chain = wd.get("owners") or []
        ownership = wd.get("ownership") or "neznámé"
        if ownership == "neznámé" and any(re.search(r"(?i)government|ministry|state", o) for o in owners_iptv):
            ownership = "státní"
        elif ownership == "neznámé" and any(re.search(r"(?i)\b(gmbh|corp|corporation|inc|llc|ltd|plc|s\.?a\.?|a\.?\s?s\.?|s\.r\.o\.?|group|media house|holdings?)\b", o)
                                            for o in owners_iptv):
            ownership = "soukromé"
        manual_ownership = str(user.get("vlastnictvi_rucne") or "").strip()
        if manual_ownership:
            ownership = manual_ownership
        it["ownership"] = ownership
        top_owner = owner_name(chain, owners_iptv)
        it["owner"] = top_owner
        website = meta.get("website") or (wd.get("websites") or [""])[0]
        in_list = it["in_list"]
        listed = [v for v in it["versions"] if v["in_list"]]
        status = "není v seznamu"
        if listed:
            ok = any(v["status"] == "ok" and v["enabled"] for v in listed)
            status = "hraje" if ok else ("vypnuto" if not any(v["enabled"] for v in listed) else "nehraje")
        last_ok = max((v["last_ok"] for v in listed if v["last_ok"]), default=None)
        country = COUNTRY_CS.get(it["country"], it["country"])
        qid = user.get("wikidata") if user.get("wikidata_overeno") else wd.get("qid")
        match = "potvrzeno" if user.get("wikidata_overeno") else wd.get("match", "žádná")

        fm = [
            "---", "typ: kanál", f"tvg_id: {q(it['versions'][0]['tvg_id'])}",
            f"skupiny: {q(it['groups'])}", f"zeme: {q(country)}", f"jazyky: {q(it['languages'])}",
            f"kategorie: {q(it['categories'])}", f"v_mem_seznamu: {q(in_list)}", f"stav: {q(status)}",
            f"oficialni_zdroj: {q(not it['restream'])}", f"platformy: {q(it['platforms'])}",
            f"program: {q(it['has_epg'])}", f"vlastnik: {q(top_owner)}", f"vlastnictvi: {q(ownership)}",
            f"vlastnictvi_rucne: {q(manual_ownership)}",
            f"zalozeno: {q((meta.get('launched') or '')[:4] or wd.get('inception', ''))}",
            f"web: {q(website or '')}", f"wikidata: {q(qid or '')}",
            f"wikidata_overeno: {q(bool(user.get('wikidata_overeno')))}", f"wikidata_shoda: {q(match)}",
            f"aktualizovano: {q(self.today)}", "tags: [\"iptv/kanál\"]", "---", "",
        ]
        body = [f"# {it['title']}", ""]
        if it["logo"]:
            body += [f'<img src="{it["logo"]}" alt="" width="140">', ""]
        flags = ["**V mém seznamu**" if in_list else "Jen ve výběru Doporučené",
                 status if in_list else None,
                 f"naposledy hrál {fmt_date(last_ok)}" if in_list and last_ok else None,
                 "má program" if it["has_epg"] else "bez programu",
                 "oficiální zdroj" if not it["restream"] else "**restream**"]
        body += ["> [!info] Stav", "> " + " · ".join(f for f in flags if f), ""]

        body += ["## O kanálu", ""]
        summ = wd.get("summary")
        if suggestion:
            ss = suggestion.get("summary")
            body += [f"> [!question] Je to tenhle kanál? Wikidata navrhují: {suggestion.get('label', '')}"]
            if suggestion.get("description"):
                body.append(f"> *{suggestion['description']}*")
            if ss:
                lang = "česká" if ss["lang"] == "cs" else "anglická"
                body += [">", f"> {ss['extract']}", ">",
                         f"> *Zdroj: [{lang} Wikipedie, {ss['title']}]({ss['url']}), licence CC BY-SA 4.0.*"]
            body.append("")
        elif summ:
            lang = "česká" if summ["lang"] == "cs" else "anglická"
            body += [summ["extract"], "",
                     f"*Zdroj: [{lang} Wikipedie, {summ['title']}]({summ['url']}), licence CC BY-SA 4.0.*", ""]
        elif wd.get("description"):
            body += [wd["description"][:1].upper() + wd["description"][1:] + ".", ""]
        facts = []
        if chain:
            facts.append("**Vlastnický řetězec (Wikidata):** " + " → ".join(
                self.link(INDEXES, "Podle vlastníků", c["name"], heading=top_owner) if c["name"] == top_owner else c["name"]
                for c in chain))
        if owners_iptv:
            facts.append("**Vlastník podle iptv-org:** " + ", ".join(owners_iptv))
        if meta.get("network"):
            facts.append(f"**Síť:** {meta['network']}")
        facts.append(f"**Typ vlastnictví:** {ownership}" + (" (nastaveno ručně)" if manual_ownership else
                     " (odhad podle Wikidat a iptv-org, opravíte klíčem `vlastnictvi_rucne`)" if ownership != "neznámé" else ""))
        if country:
            facts.append(f"**Země:** {self.link(INDEXES, 'Podle zemí', country, heading=country)}")
        if it["languages"]:
            facts.append("**Jazyky:** " + ", ".join(it["languages"]))
        if it["categories"]:
            facts.append("**Kategorie:** " + ", ".join(it["categories"]))
        founded = (meta.get("launched") or "")[:4] or wd.get("inception", "")
        if founded:
            facts.append(f"**Založeno:** {founded}")
        if wd.get("headquarters"):
            facts.append(f"**Sídlo:** {wd['headquarters']}")
        if website:
            facts.append(f"**Web:** <{website}>")
        if meta.get("alt_names"):
            facts.append("**Jiné názvy:** " + ", ".join(meta["alt_names"][:6]))
        if qid:
            hint = {"jistá": "shoda ověřená přes oficiální web", "k ověření": "**shodu je potřeba ověřit**",
                    "ručně": "nastaveno ručně", "potvrzeno": "potvrzeno ručně"}.get(match, match)
            facts.append(f"**Wikidata:** [{qid}](https://www.wikidata.org/wiki/{qid}), {hint}")
        else:
            facts.append("**Wikidata:** nenalezeno")
        body += [f"- {f}" for f in facts] + [""]
        if match == "k ověření":
            body += ["> [!question] Sedí položka z Wikidat?",
                     "> Když ano, nastavte v hlavičce `wikidata_overeno: true`. Když ne, přepište `wikidata`",
                     "> na správné Q… (nebo na `\"žádný\"`) a také nastavte `wikidata_overeno: true`.", ""]

        body += ["## Verze a streamy", "", "| Verze | V seznamu | Skupina | Zdroj streamu | Program |",
                 "|---|---|---|---|---|"]
        for v in it["versions"]:
            src = ", ".join(self.link(SOURCES, p, in_table=True) for p in platforms(v["url"], self.cfg))
            host = urlparse(v["url"]).hostname or ""
            body.append(f"| {v['name']} | {'ano' if v['in_list'] else 'ne'} | {v['group'] or '–'} | "
                        f"{src} (`{host}`) | {'ano' if v['has_epg'] else 'ne'} |")
        body.append("")
        if it["siblings"]:
            body += ["Další verze v katalogu: " + ", ".join(it["siblings"]) + ".", ""]

        body += ["## Program", ""]
        with_src = [v for v in listed if v["epg_source"]]
        if with_src:
            for v in with_src:
                host = urlparse(v["epg_source"]).hostname or ""
                page_src = "i.mjh.nz" if "mjh" in host or "githubusercontent" in host else (
                    "epgshare01" if "epgshare" in host else host)
                body.append(f"- {v['name']}: {v['epg_count']} pořadů z {self.link(SOURCES, page_src) if page_src in SOURCE_PAGES else page_src}")
        elif it["has_epg"]:
            body.append("- Program existuje. Stáhne se, až kanál přidáte do seznamu.")
        else:
            body.append("- Program pro tento kanál není k dispozici.")
        body.append("")

        body += ["## Přehrát", ""]
        for v in listed:
            body.append(f"- [{v['name']} v přehrávači]({self.lan}/?ch={v['channel_id']})")
        if not listed:
            body.append(f"- Kanál zatím nemáte v seznamu. [Přidat na stránce Přidat kanály]({self.lan}/sprava?q={quote(it['title'])})")
        body += ["", notes_block(self._notes)]
        return "\n".join(fm + body)

    async def build(self, use_wikidata: bool = True) -> dict:
        iptv = await load_iptv_channels(self.cfg)
        items = collect(self.con, self.cfg, iptv)

        # názvy stránek: unikátní podle titulu
        pages: dict[str, str] = {}
        used: dict[str, str] = {}
        for key, it in sorted(items.items(), key=lambda kv: kv[1]["title"].lower()):
            name = safe_name(it["title"])
            if name.lower() in used and used[name.lower()] != key:
                name = safe_name(f"{it['title']} ({it['country'] or key})")
            used[name.lower()] = key
            pages[key] = name

        chdir = self.root / CHANNELS
        existing = {key: read_existing(chdir / f"{pages[key]}.md") for key in items}
        overrides = {k: str(v[1]["wikidata"]) for k, v in existing.items()
                     if v[1].get("wikidata_overeno") and v[1].get("wikidata")}
        wd: dict[str, dict] = {}
        if use_wikidata:
            req = [{"key": k, "names": [it["meta"].get("name") or it["title"]] + (it["meta"].get("alt_names") or [])
                    + [it["title"]], "website": it["meta"].get("website")} for k, it in items.items()]
            wd = await wikidata.enrich(self.con, req, overrides)
        else:
            raw = db.get_meta(self.con, "wiki_wikidata")
            wd = json.loads(raw) if raw else {}
        db.set_meta(self.con, "wiki_wikidata", json.dumps(wd, ensure_ascii=False))

        changed = 0
        written: set[Path] = set()
        for key, it in items.items():
            path = chdir / f"{pages[key]}.md"
            self._notes, user = existing[key]
            changed += write_if_changed(path, self.channel_page(it, pages[key], wd.get(key, {}), user))
            written.add(path)
        gone = self._mark_gone(chdir, written)

        changed += self._blocked_sources_stub()
        changed += self._sources(items, pages)
        changed += self._indexes(items, pages, wd)
        changed += self._home(items, wd)
        self._store_links(items, pages)

        summary = {
            "pages": len(items), "changed": changed, "gone": gone,
            "wikidata_sure": sum(1 for k in items if wd.get(k, {}).get("match") == "jistá"),
            "wikidata_check": sum(1 for k in items if wd.get(k, {}).get("match") == "k ověření"),
            "wikidata_none": sum(1 for k in items if wd.get(k, {}).get("match", "žádná").startswith("žádná")),
        }
        db.set_meta(self.con, "last_wiki", db.now())
        db.log(self.con, "Wiki: {pages} kanálů, {changed} stránek změněno, Wikidata jistě {wikidata_sure}, "
                         "k ověření {wikidata_check}".format(**summary))
        self.con.commit()
        return summary

    def _mark_gone(self, chdir: Path, written: set[Path]) -> int:
        n = 0
        for p in chdir.glob("*.md") if chdir.exists() else []:
            if p in written:
                continue
            text = p.read_text(encoding="utf-8")
            if GONE_MARK not in text:
                parts = text.split("---", 2)
                note = f"\n{GONE_MARK} (od {self.today}). Stránka zůstává kvůli vašim poznámkám.\n"
                text = (f"---{parts[1]}---{note}{parts[2]}" if text.startswith("---") and len(parts) == 3
                        else note + text)
                p.write_text(text, encoding="utf-8")
            n += 1
        return n

    def _page(self, path: Path, head: list[str], body: list[str]) -> int:
        notes, _ = read_existing(path)
        return int(write_if_changed(path, "\n".join(head + body + ["", notes_block(notes)])))

    def _blocked_sources_stub(self) -> int:
        """Stránku Blokované zdroje píše uživatel ručně. Když chybí, založí se základní verze."""
        path = self.root / SOURCES / "Blokované zdroje.md"
        if path.exists():
            return 0
        head = ["---", "typ: zdroj", "tags: [\"iptv/zdroj\"]", "---", "", "# Blokované zdroje", ""]
        body = ["Zdroje vyřazené podle vlastního nastavení `blocked_hosts` a `block_ip_hosts`.", "Veřejná distribuce neobsahuje seznam blokovaných streamů ani osobní playlisty.", ""]
        return self._page(path, head, body)

    def _sources(self, items: dict, pages: dict) -> int:
        by_platform: dict[str, list[str]] = defaultdict(list)
        for key, it in items.items():
            for p in it["platforms"]:
                by_platform[p].append(key)
        n = 0
        for name, text in SOURCE_PAGES.items():
            head = ["---", "typ: zdroj", f"aktualizovano: {q(self.today)}", "tags: [\"iptv/zdroj\"]", "---", "", f"# {name}", ""]
            body = [text.replace("{prefix}", self.prefix), ""]
            keys = by_platform.get(name, [])
            if keys:
                body += ["## Kanály z tohoto zdroje", ""]
                body += [f"- {self.link(CHANNELS, pages[k], items[k]['title'])}" + (" (v seznamu)" if items[k]["in_list"] else "")
                         for k in sorted(keys, key=lambda k: items[k]["title"].lower())]
                body.append("")
            n += self._page(self.root / SOURCES / f"{name}.md", head, body)
        return n

    def _indexes(self, items: dict, pages: dict, wd: dict) -> int:
        def line(k):
            it = items[k]
            tag = " ★" if any(v["favorite"] for v in it["versions"]) else ""
            return f"- {self.link(CHANNELS, pages[k], it['title'])}{tag}" + (" · v seznamu" if it["in_list"] else "")
        n = 0
        groups: dict[str, list[str]] = defaultdict(list)
        for k, it in items.items():
            for g in it["groups"] or ["Bez skupiny"]:
                groups[g].append(k)
        body = ["Kanály podle skupin. Skupina pochází z mého seznamu, nebo z výběru Doporučené.", ""]
        for g in sorted(groups):
            body += [f"## {g}", ""] + [line(k) for k in sorted(groups[g], key=lambda k: items[k]["title"].lower())] + [""]
        n += self._page(self.root / INDEXES / "Podle skupin.md", self._ihead("Podle skupin"), body)

        countries: dict[str, list[str]] = defaultdict(list)
        for k, it in items.items():
            countries[COUNTRY_CS.get(it["country"], it["country"]) or "Neznámá země"].append(k)
        body = []
        for c in sorted(countries):
            body += [f"## {c}", ""] + [line(k) for k in sorted(countries[c], key=lambda k: items[k]["title"].lower())] + [""]
        n += self._page(self.root / INDEXES / "Podle zemí.md", self._ihead("Podle zemí"), body)

        owners: dict[str, list[str]] = defaultdict(list)
        for k, it in items.items():
            o = it.get("owner") or "Neznámý vlastník"
            owners[o].append(k)
        body = ["Nejvyšší známý vlastník podle Wikidat, jinak podle iptv-org. Skupiny s více kanály jsou nahoře.", ""]
        for o in sorted(owners, key=lambda o: (o == "Neznámý vlastník", -len(owners[o]), o.lower())):
            body += [f"## {o}", ""] + [line(k) for k in sorted(owners[o], key=lambda k: items[k]["title"].lower())] + [""]
        n += self._page(self.root / INDEXES / "Podle vlastníků.md", self._ihead("Podle vlastníků"), body)

        kinds: dict[str, list[str]] = defaultdict(list)
        for k, it in items.items():
            kind = it.get("ownership", "neznámé")
            if kind in ("státní", "veřejnoprávní"):
                kinds[kind].append(k)
        body = ["Odhad podle typu stanice a jejích vlastníků ve Wikidatech a v iptv-org. Neberte ho jako definitivní,",
                "hranice mezi státní a veřejnoprávní stanicí se v různých zemích liší.", ""]
        for kind in ("veřejnoprávní", "státní"):
            body += [f"## {kind.capitalize()}", ""] + [line(k) for k in sorted(kinds[kind], key=lambda k: items[k]["title"].lower())] + [""]
        n += self._page(self.root / INDEXES / "Státní a veřejnoprávní.md", self._ihead("Státní a veřejnoprávní"), body)
        return n

    def _ihead(self, title: str) -> list[str]:
        return ["---", "typ: rozcestník", f"aktualizovano: {q(self.today)}", "tags: [\"iptv/rozcestník\"]", "---", "",
                f"# {title}", ""]

    def _home(self, items: dict, wd: dict) -> int:
        lan = lan_addresses(self.cfg.port)
        in_list = sum(1 for it in items.values() if it["in_list"])
        check = sum(1 for k in items if wd.get(k, {}).get("match") == "k ověření")
        L = lambda folder, page, label=None: self.link(folder, page, label)
        body = [
            "Osobní IPTV: vlastní seznam kanálů z oficiálních zdrojů, přehrávač na webu a playlist pro Apple TV.",
            "",
            "## Jak to funguje",
            "",
            "1. **Server** běží na Macu (`appletv/server`). Drží seznam kanálů, jednou denně kontroluje streamy,",
            "   dvakrát denně stahuje program a jednou týdně obnovuje katalog kanálů z iptv-org, Free-TV a i.mjh.nz.",
            f"2. **Web** na <{lan.get('ip_url') or self.lan}>: Přehrávač, Přidat kanály, Moje kanály, Wiki a Nastavení.",
            f"3. **Apple TV** (iPlayTV, UHF) a VLC berou playlist z <{(lan.get('ip_url') or self.lan)}/playlist.m3u>.",
            f"4. **Tahle wiki** se generuje ze serveru. Čte se v Obsidianu i na webu (<{lan.get('ip_url') or self.lan}/wiki>),",
            "   poznámky se píšou v Obsidianu pod nadpis Moje poznámky a zůstávají.",
            "",
            "## Adresy",
            "",
            f"- **Web v domácí síti:** <{lan.get('ip_url') or self.lan}>",
            f"- **Stálé jméno Macu:** <{lan.get('local_url') or '–'}>",
            f"- **Playlist:** <{lan.get('ip_url') or self.lan}/playlist.m3u>",
            f"- **Program (XMLTV):** <{lan.get('ip_url') or self.lan}/epg.xml.gz>",
            "",
            "## Přehled",
            "",
            f"- **Kanálů ve wiki:** {len(items)}, z toho v mém seznamu {in_list}",
            f"- **Wikidata k ověření:** {check}" + (" (stránky se štítkem „shodu je potřeba ověřit“)" if check else ""),
            "",
            "## Rozcestníky",
            "",
            f"- {L(INDEXES, 'Podle skupin')}",
            f"- {L(INDEXES, 'Podle zemí')}",
            f"- {L(INDEXES, 'Podle vlastníků')}",
            f"- {L(INDEXES, 'Státní a veřejnoprávní')}",
            "",
            "## Zdroje",
            "",
            "Odkud streamy a program pocházejí a co znamenají názvy serverů:",
            "",
        ] + [f"- {L(SOURCES, s)}" for s in SOURCE_PAGES] + [
            "",
            "## Spuštění serveru",
            "",
            "```",
            "cd /cesta/k/moje-tv && ./run.sh",
            "```",
            "",
            "Automatické spouštění po startu Macu a další příkazy jsou v `README.md`.",
            "",
            "## Zásady pro zdroje",
            "",
            "- Běžný katalog a automatické opravy vyřazují blokované zdroje (`blocked_hosts`).",
            "- Restreamy volně vysílaných kanálů jen označuje (`restream_hosts`, skupina Restreamy).",
            "- Nové kanály si vybírám sám na stránce Přidat kanály, ověřený výběr je pod ★ Doporučené.",
        ]
        head = ["---", "typ: úvod", f"aktualizovano: {q(self.today)}", "tags: [\"iptv\"]", "---", "", "# Moje TV", ""]
        return self._page(self.root / "Moje TV.md", head, body)

    def _store_links(self, items: dict, pages: dict) -> None:
        self.con.execute("CREATE TABLE IF NOT EXISTS wiki_links (url TEXT PRIMARY KEY, page TEXT NOT NULL)")
        self.con.execute("DELETE FROM wiki_links")
        rows = [(v["url"], f"{CHANNELS}/{pages[k]}") for k, it in items.items() for v in it["versions"]]
        self.con.executemany("INSERT OR REPLACE INTO wiki_links (url, page) VALUES (?, ?)", rows)


async def build(con: sqlite3.Connection, cfg: Config, use_wikidata: bool = True) -> dict:
    return await Wiki(con, cfg).build(use_wikidata)


def check_links(root: Path, prefix: str) -> list[tuple[str, str]]:
    """Najde odkazy [[…]] na neexistující stránky nebo nadpisy. Vrací (soubor, cíl)."""
    bad = []
    vault_root = root
    for _ in prefix.strip("/").split("/"):
        vault_root = vault_root.parent
    headings: dict[Path, set[str]] = {}
    for p in root.rglob("*.md"):
        for raw in re.findall(r"\[\[([^\]]+)\]\]", p.read_text(encoding="utf-8")):
            target = re.split(r"\\?\|", raw, maxsplit=1)[0]
            page, _, heading = target.partition("#")
            path = vault_root / f"{page}.md"
            if not path.exists():
                bad.append((str(p.relative_to(root)), target))
                continue
            if heading:
                if path not in headings:
                    headings[path] = set(re.findall(r"^#+\s+(.+?)\s*$", path.read_text(encoding="utf-8"), re.M))
                if heading not in headings[path]:
                    bad.append((str(p.relative_to(root)), target))
    return bad
