"""Příkazová řádka: tvserver import | check | list | export | serve."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import httpx

from . import catalog, checker, db, enrichment, epg, m3u, wiki
from .config import load


def _read_source(src: str) -> str:
    if src.startswith(("http://", "https://")):
        r = httpx.get(src, follow_redirects=True, timeout=60)
        r.raise_for_status()
        return r.text
    return Path(src).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tvserver", description="Osobní IPTV server")
    ap.add_argument("--config", help="cesta ke config.toml (výchozí: ./config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import", help="přidá kanály ze souboru nebo URL s M3U")
    p.add_argument("source")
    p = sub.add_parser("check", help="zkontroluje streamy hned teď")
    p.add_argument("--no-repair", action="store_true", help="nezkoušet hledat náhradní adresy")
    sub.add_parser("list", help="vypíše kanály a jejich stav")
    sub.add_parser("catalog", help="obnoví katalog kanálů z iptv-org")
    p = sub.add_parser("catalog-metadata", help="doplní metadata nespárovaných streamů")
    p.add_argument("--dry-run", action="store_true", help="jen připravit přehled shod bez změn databáze")
    p = sub.add_parser("wiki", help="vygeneruje wiki o kanálech pro Obsidian")
    p.add_argument("--no-wikidata", action="store_true", help="nedotazovat Wikidata, použít poslední údaje")
    p = sub.add_parser("epg", help="stáhne program hned teď")
    p.add_argument("--status", action="store_true", help="jen vypíše, odkud má který kanál program")
    p = sub.add_parser("export", help="uloží aktuální seznam jako M3U")
    p.add_argument("file")
    sub.add_parser("serve", help="spustí webový server")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load(a.config)

    if a.cmd == "serve":
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)
        return 0

    con = db.connect(cfg.database)
    if a.cmd == "import":
        entries = m3u.parse(_read_source(a.source))
        added, updated = db.import_entries(con, entries)
        print(f"Přidáno {added}, aktualizováno {updated} kanálů.")
    elif a.cmd == "check":
        s = asyncio.run(checker.run_check(con, cfg, repair=not a.no_repair))
        print(f"Zkontrolováno {s['checked']}: {s['ok']} v pořádku, {s['repaired']} opraveno, "
              f"{s['dead']} nefunkčních, {s['hidden']} nově skryto.")
    elif a.cmd == "list":
        for r in db.all_channels(con):
            mark = {"ok": "OK  ", "dead": "DEAD", "unknown": "?   "}[r["status"]]
            off = "" if r["enabled"] else " (vypnuto)"
            print(f"{mark} {r['grp']:8} {r['name']}{off}  {r['detail']}")
    elif a.cmd == "catalog":
        s = asyncio.run(catalog.refresh(con, cfg))
        print(f"Katalog má {s['streams']} streamů z iptv-org"
              + "".join(f", {n} z {name}" for name, n in s["lists"].items()) + ".")
    elif a.cmd == "catalog-metadata":
        s = asyncio.run(enrichment.refresh(con, cfg, dry_run=a.dry_run))
        print(f"Metadata: {s['identified']} nově spárovaných streamů, {s['enriched']} doplněno, "
              f"{s['unmatched_after']} zůstává nespárovaných.")
        print(f"Přehled: {s['report']}")
    elif a.cmd == "wiki":
        s = asyncio.run(wiki.build(con, cfg, use_wikidata=not a.no_wikidata))
        print(f"Wiki: {s['pages']} kanálů, {s['changed']} stránek změněno, {s['gone']} kanálů už není ve výběru.")
        print(f"Wikidata: jistá shoda {s['wikidata_sure']}, k ověření {s['wikidata_check']}, nenalezeno {s['wikidata_none']}.")
        bad = wiki.check_links(cfg.wiki_dir, cfg.obsidian_prefix)
        print(f"Odkazy: {'všechny vedou na existující stránky' if not bad else f'{len(bad)} nefunkčních'}")
        for f, t in bad[:20]:
            print(f"  {f} -> {t}")
        print(f"Složka: {cfg.wiki_dir}")
    elif a.cmd == "epg":
        if not a.status:
            s = asyncio.run(epg.refresh(con, cfg))
            print(f"Program má {s['with_epg']} z {s['channels']} kanálů, celkem {s['programmes']} pořadů."
                  + (f" Nepodařilo se stáhnout {s['failed_sources']} zdrojů." if s["failed_sources"] else ""))
        rows = con.execute("SELECT c.name, e.how, e.count, e.source FROM channels c "
                           "LEFT JOIN epg_status e ON e.channel_id = c.id ORDER BY c.sort, c.id")
        for r in rows:
            src = (r["source"] or "").split("/")[2] if r["source"] else ""
            print(f"{r['count'] or 0:4} {r['how'] or '-':6} {m3u.clean_name(r['name'])[:30]:30} {src}")
    elif a.cmd == "export":
        rows = db.visible(con, cfg.hide_after_failures)
        Path(a.file).write_text(m3u.render([db.to_entry(r) for r in rows]), encoding="utf-8")
        print(f"Uloženo {len(rows)} kanálů do {a.file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
