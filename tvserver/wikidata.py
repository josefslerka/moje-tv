"""Doplnění kanálů o údaje z Wikidat a Wikipedie.

Párování kanálu s položkou Wikidat:
- jistá shoda: oficiální web z Wikidat (P856) má stejnou doménu jako web z iptv-org,
- k ověření: položka je televizní kanál nebo vysílatel a sedí název, ale web chybí nebo nesedí,
- žádná: nic vhodného se nenašlo.

Odpovědi se ukládají do mezipaměti v databázi, aby se Wikidata nedotazovala zbytečně často.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from urllib.parse import quote, urlparse

import httpx

log = logging.getLogger("tvserver.wikidata")

API = "https://www.wikidata.org/w/api.php"
UA = "tvserver-wiki/0.1 (osobni IPTV wiki; https://github.com/iptv-org)"
CACHE_DAYS = 30
TV_WORDS = ("television", "tv channel", "broadcaster", "broadcasting", "news channel", "cable channel",
            "streaming", "specialty channel", "network", "media company", "radio")
PUBLIC_WORDS = ("public broadcaster", "public service", "public broadcasting", "public-service",
                "public television", "public radio", "public state broadcaster", "regional public", "veřejnoprávní")
# Silné státní signály. Samotné „state broadcaster“ sem nepatří: německé zemské stanice
# mají typ „german public state broadcaster“ a jsou veřejnoprávní.
STATE_STRONG = ("state-owned", "state owned", "government", "ministry", "state media", "státní", "state agency")
COMPANY_WORDS = ("company", "corporation", "business", "conglomerate", "enterprise", "holding", "plc",
                 "investment", "firm", "podnik", "společnost")
STATE_WORDS = ("state media", "state-owned", "state owned", "government", "ministry", "state broadcaster",
               "federal government", "state agency")
SECOND_LEVEL = {"co", "com", "net", "org", "gov", "ac", "edu"}


def _ensure_cache(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS http_cache (url TEXT PRIMARY KEY, body TEXT NOT NULL, ts REAL NOT NULL)")


async def _get_json(client: httpx.AsyncClient, con: sqlite3.Connection, url: str, params: dict | None = None):
    key = url + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "")
    row = con.execute("SELECT body, ts FROM http_cache WHERE url = ?", (key,)).fetchone()
    if row and time.time() - row["ts"] < CACHE_DAYS * 86400:
        return json.loads(row["body"])
    for attempt in range(3):
        try:
            r = await client.get(url, params=params)
            if r.status_code == 429:
                await asyncio.sleep(2 + attempt * 3)
                continue
            if r.status_code == 404:
                data = None
            else:
                r.raise_for_status()
                data = r.json()
            con.execute("INSERT OR REPLACE INTO http_cache (url, body, ts) VALUES (?,?,?)",
                        (key, json.dumps(data), time.time()))
            return data
        except httpx.HTTPError as e:
            log.warning("Dotaz %s selhal: %s", url, e)
            await asyncio.sleep(1 + attempt)
    return None


def domain(url: str | None) -> str:
    """Registrovatelná doména, např. www.france24.com -> france24.com, abc.net.au -> abc.net.au."""
    host = (urlparse(url or "").hostname or "").lower()
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and parts[-2] in SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", s.casefold())


def _claims(ent: dict, prop: str) -> list:
    out = []
    for c in ent.get("claims", {}).get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if v is not None:
            out.append(v)
    return out


def _ids(ent: dict, prop: str) -> list[str]:
    return [v["id"] for v in _claims(ent, prop) if isinstance(v, dict) and "id" in v]


def _label(ent: dict | None) -> str:
    if not ent:
        return ""
    lab = ent.get("labels", {})
    return (lab.get("cs") or lab.get("en") or next(iter(lab.values()), {})).get("value", "")


COUNTRY_WORDS = ("country", "sovereign state", "federal state", "republic", "kingdom", "state of",
                 "suverénní stát", "země", "stát |", "government", "vláda")


def classify(own_types: str, chain: list[dict]) -> str:
    """Odhad typu vlastnictví. Pořadí pravidel je důležité:
    1. vlastník je výslovně státní nebo vládní (Rossija 24 -> VGTRK): státní,
    2. stanice nebo vlastník je veřejnoprávní vysílatel (WDR, ARD, BBC): veřejnoprávní,
    3. řetězec vlastníků končí u státu či vlády (France 24 -> Francie): státní,
    4. stanice je vedená jako státní médium: státní,
    5. některý vlastník je firma: soukromé.
    """
    clean = lambda t: t.replace("intergovernmental", "").replace("mezivládní", "")
    owners = [clean(c["types"] + " | " + c["name"].lower()) for c in chain]
    own_types = clean(own_types)
    if any(w in t for t in owners for w in STATE_STRONG):
        return "státní"
    if any(w in t for t in [own_types] + owners for w in PUBLIC_WORDS):
        return "veřejnoprávní"
    if chain and any(w in (clean(chain[-1]["types"]) + " |") for w in COUNTRY_WORDS):
        return "státní"
    if any(w in own_types for w in STATE_WORDS):
        return "státní"
    if any(w in t for t in owners for w in COMPANY_WORDS):
        return "soukromé"
    return "neznámé"


class Wikidata:
    def __init__(self, con: sqlite3.Connection, client: httpx.AsyncClient):
        self.con, self.client = con, client
        self.entities: dict[str, dict] = {}
        _ensure_cache(con)

    async def search(self, text: str) -> list[str]:
        ids: list[str] = []
        for lang in ("en", "cs"):
            data = await _get_json(self.client, self.con, API, {
                "action": "wbsearchentities", "search": text, "language": lang, "uselang": lang,
                "type": "item", "limit": "7", "format": "json"})
            for hit in (data or {}).get("search", []):
                if hit["id"] not in ids:
                    ids.append(hit["id"])
        return ids

    async def fetch(self, ids: list[str]) -> None:
        missing = [i for i in dict.fromkeys(ids) if i and i not in self.entities]
        for i in range(0, len(missing), 40):
            chunk = missing[i:i + 40]
            data = await _get_json(self.client, self.con, API, {
                "action": "wbgetentities", "ids": "|".join(chunk), "format": "json",
                "props": "claims|labels|descriptions|sitelinks", "languages": "cs|en|de|fr|sk|es|it|pl"})
            for qid, ent in ((data or {}).get("entities") or {}).items():
                if "missing" not in ent:
                    self.entities[qid] = ent

    def get(self, qid: str) -> dict | None:
        return self.entities.get(qid)

    async def types_text(self, ent: dict, with_description: bool = True) -> str:
        """Typ položky (P31), případně i její popisek, malými písmeny, pro hledání klíčových slov."""
        t = _ids(ent, "P31")
        await self.fetch(t)
        words = [_label(self.get(x)).lower() for x in t]
        words += [(self.get(x) or {}).get("labels", {}).get("en", {}).get("value", "").lower() for x in t]
        if with_description:
            words.append(ent.get("descriptions", {}).get("en", {}).get("value", "").lower())
        return " | ".join(w for w in words if w)

    async def match(self, names: list[str], website: str | None) -> tuple[str | None, str, int]:
        """Najde položku pro kanál. Vrací (QID, shoda, skóre)."""
        cands: list[str] = []
        for n in names[:3]:
            for q in await self.search(n):
                if q not in cands:
                    cands.append(q)
        cands = cands[:15]
        await self.fetch(cands)
        want = domain(website) if website else ""
        best, best_score, best_sure = None, 0, False
        normed = {_norm(n) for n in names if n}
        for q in cands:
            ent = self.get(q)
            if not ent:
                continue
            sites = {domain(u) for u in _claims(ent, "P856") if isinstance(u, str)}
            site_ok = bool(want) and want in sites
            types = await self.types_text(ent)
            tv_ok = any(w in types for w in TV_WORDS)
            labels = {_norm(v.get("value", "")) for v in ent.get("labels", {}).values()}
            name_ok = bool(labels & normed)
            # Jistá shoda: sedí web i název. Stejný web mívá víc kanálů jedné stanice (Óčko, Óčko Star).
            sure = site_ok and name_ok
            score = (100 if sure else 0) + (50 if site_ok else 0) + (40 if tv_ok else 0) + (20 if name_ok else 0)
            if score > best_score:
                best, best_score, best_sure = q, score, sure
        if best_sure:
            return best, "jistá", best_score
        if best_score >= 50:
            return best, "k ověření", best_score
        return None, "žádná", best_score

    async def owner_chain(self, ent: dict, depth: int = 4) -> list[dict]:
        """Vlastníci od přímého po nejvyššího.

        V prvním kroku se bere vlastník (P127), nebo mateřská organizace (P749). Dál už jen
        vlastník: mateřská organizace se ve Wikidatech používá volně a vede k nesmyslům
        (DW -> ARD -> Bayerischer Rundfunk -> Bavorsko).
        """
        chain, seen, cur = [], {ent.get("id")}, ent
        for step in range(depth):
            nxt = _ids(cur, "P127") or (_ids(cur, "P749") if step == 0 else [])
            nxt = [n for n in nxt if n not in seen]
            # Víc vlastníků najednou (sdružení jako ARD) nemá jednoho nadřízeného, řetězec tu končí.
            if not nxt or (step > 0 and len(nxt) > 1):
                break
            await self.fetch(nxt[:1])
            cur = self.get(nxt[0])
            if not cur:
                break
            seen.add(nxt[0])
            chain.append({"qid": nxt[0], "name": _label(cur) or nxt[0], "types": await self.types_text(cur)})
        return chain

    async def summary(self, ent: dict) -> dict | None:
        """První odstavec z Wikipedie, česky, jinak anglicky."""
        links = ent.get("sitelinks", {})
        for lang in ("cs", "en"):
            title = links.get(f"{lang}wiki", {}).get("title")
            if not title:
                continue
            data = await _get_json(self.client, self.con,
                                   f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}")
            if data and data.get("extract"):
                return {"lang": lang, "title": title, "extract": data["extract"].strip(),
                        "url": data.get("content_urls", {}).get("desktop", {}).get("page")
                        or f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"}
        return None

    async def facts(self, qid: str) -> dict:
        await self.fetch([qid])
        ent = self.get(qid)
        if not ent:
            return {}
        ref = _ids(ent, "P17") + _ids(ent, "P159") + _ids(ent, "P127") + _ids(ent, "P749")
        await self.fetch(ref)
        inception = next((v.get("time", "")[1:5] for v in _claims(ent, "P571") if isinstance(v, dict)), "")
        logo = next((v for v in _claims(ent, "P154") if isinstance(v, str)), "")
        chain = await self.owner_chain(ent)
        types = await self.types_text(ent)
        texts = [types] + [c["types"] + " | " + c["name"].lower() for c in chain]
        ownership = classify(types, chain)
        return {
            "qid": qid,
            "label": _label(ent) or qid,
            "description": (ent.get("descriptions", {}).get("cs") or ent.get("descriptions", {}).get("en") or {}).get("value", ""),
            "inception": inception if inception.isdigit() else "",
            "country": ", ".join(_label(self.get(x)) for x in _ids(ent, "P17")),
            "headquarters": ", ".join(_label(self.get(x)) for x in _ids(ent, "P159")),
            "websites": [u for u in _claims(ent, "P856") if isinstance(u, str)][:2],
            "logo": f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(logo.replace(' ', '_'))}?width=240" if logo else "",
            "owners": chain,
            "ownership": ownership,
            "summary": await self.summary(ent),
        }


async def enrich(con: sqlite3.Connection, items: list[dict], overrides: dict[str, str]) -> dict[str, dict]:
    """Pro každý kanál vrátí údaje z Wikidat.

    items: [{"key", "names": [...], "website"}]
    overrides: {key: QID nebo "žádný"} z hlaviček stránek, které uživatel upravil ručně.
    """
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20, follow_redirects=True) as client:
        wd = Wikidata(con, client)
        sem = asyncio.Semaphore(4)

        async def one(it):
            async with sem:
                key = it["key"]
                ov = overrides.get(key)
                if ov and ov.lower() in ("žádný", "zadny", "none", "-"):
                    out[key] = {"match": "žádná (ručně)"}
                    return
                if ov and re.fullmatch(r"Q\d+", ov):
                    qid, match, score = ov, "ručně", 0
                else:
                    qid, match, score = await wd.match(it["names"], it.get("website"))
                data = await wd.facts(qid) if qid else {}
                out[key] = {"match": match, "score": score, **data}
        await asyncio.gather(*(one(it) for it in items))
    con.commit()
    return out
