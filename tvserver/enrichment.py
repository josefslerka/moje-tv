"""Doplnění metadat již uloženého katalogu se záznamem důvodů každé shody."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import httpx

from . import catalog, db, m3u
from .checker import base_id
from .config import Config
from .epg import load_guides


def enrich(con, cfg: Config, api: dict[str, list], playlists: list[tuple[dict, list[m3u.Entry]]],
           guides: list[dict], dry_run: bool = False, report_path: Path | None = None) -> dict:
    expanded = replace(cfg, blocked_hosts=[], block_ip_hosts=False)
    index = catalog.playlist_metadata_index(api, playlists, expanded, guides)
    original = {entry.url: (entry, pl.get("country", "")) for pl, entries in playlists for entry in entries}
    defaults = {pl["name"]: pl.get("country", "") for pl in cfg.catalog_playlists}
    rows = con.execute("SELECT * FROM catalog ORDER BY source, name, url").fetchall()
    batches = defaultdict(list)
    for row in rows:
        if row["url"] in original:
            entry, fallback = original[row["url"]]
        else:
            fallback = defaults.get(row["source"], "")
            # Země přidaná jen jako výchozí hodnota seznamu není důkazem původu stanice.
            attrs = {"tvg-country": row["country"]} if row["country"] and row["country"] != fallback else {}
            entry = m3u.Entry(name=row["name"], url=row["url"], tvg_id=row["tvg_id"], logo=row["logo"], attrs=attrs)
        batches[fallback].append(entry)
    proposed = {}
    for fallback, entries in batches.items():
        proposed.update({row["url"]: row for row in catalog.build_playlist(
            entries, "metadata", expanded, api, guides, country=fallback, metadata_index=index)})

    fields = ("tvg_id", "country", "categories", "languages", "logo", "has_epg")
    summary = {"streams": len(rows), "unmatched_before": 0, "identified": 0, "ambiguous": 0, "excluded_by_source_rules": 0,
               "unmatched_after": 0, "enriched": 0, "known_enriched": 0, "channels_updated": 0, "dry_run": dry_run}
    field_counts, methods, records, updates = Counter(), Counter(), [], []
    for row in rows:
        unknown = not row["tvg_id"] or base_id(row["tvg_id"]) not in index.channels
        summary["unmatched_before"] += int(unknown)
        candidate = proposed.get(row["url"])
        method = candidate["_metadata_method"] if candidate else "excluded_by_source_rules"
        matched = candidate and candidate["_metadata_matched"]
        if unknown:
            summary["identified"] += int(bool(matched))
            summary["unmatched_after"] += int(not matched)
            summary["ambiguous"] += int(method in ("ambiguous", "country_conflict", "logo_conflict"))
            summary["excluded_by_source_rules"] += int(method == "excluded_by_source_rules")
        changes = {}
        if candidate:
            for field in fields:
                if not matched and field not in candidate["_metadata_explicit"]:
                    continue
                value = candidate[field]
                if field == "categories":
                    value = ",".join(dict.fromkeys(filter(None, (row[field] + "," + value).split(","))))
                if value != row[field] and value and (unknown or not row[field] or field == "categories"):
                    changes[field] = {"from": row[field], "to": value}
        if changes:
            summary["enriched"] += 1
            summary["known_enriched"] += int(not unknown)
            field_counts.update(changes.keys())
            methods[method] += 1
            values = [changes.get(field, {}).get("to", row[field]) for field in fields]
            updates.append((row, values, changes))
        if unknown or changes:
            records.append({"stream": hashlib.sha256(row["url"].encode()).hexdigest()[:16],
                            "source": row["source"], "name": row["name"], "original_id": row["tvg_id"],
                            "matched_id": candidate["tvg_id"] if matched else "", "method": method,
                            "candidates": candidate["_metadata_candidates"] if candidate else [], "changes": changes,
                            "metadata_from": "matched_station" if matched else "source_attributes" if changes else "none"})

    summary["fields"] = dict(field_counts)
    summary["methods"] = dict(methods)
    if not dry_run:
        with con:
            for row, values, changes in updates:
                con.execute("UPDATE catalog SET tvg_id=?, country=?, categories=?, languages=?, logo=?, has_epg=? WHERE url=?",
                            (*values, row["url"]))
                if "tvg_id" in changes:
                    summary["channels_updated"] += con.execute(
                        "UPDATE channels SET tvg_id=? WHERE url=? AND tvg_id=?",
                        (values[0], row["url"], row["tvg_id"])).rowcount
                if "logo" in changes:
                    con.execute("UPDATE channels SET logo=? WHERE url=? AND logo=''", (values[4], row["url"]))
            db.log(con, f"Metadata: {summary['identified']} nově spárovaných streamů, "
                        f"{summary['enriched']} doplněno, {summary['unmatched_after']} zůstává nespárovaných")
            db.set_meta(con, "last_metadata", db.now())
        cache = cfg.database.parent / "wiki_cache" / "iptv_channels.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(api["channels"], ensure_ascii=False), encoding="utf-8")
    report_path = report_path or (cfg.database.parent / "metadata_reports" / f"metadata-{db.now().replace(':', '-')}.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({"summary": summary, "items": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary | {"report": str(report_path)}


async def refresh(con, cfg: Config, dry_run: bool = False) -> dict:
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        api = await catalog.load_api(client, cfg)
        guides = await load_guides(client, cfg)

        async def get(pl):
            response = await client.get(pl["url"])
            response.raise_for_status()
            return pl, m3u.parse_catalog(response.text)

        playlists = await asyncio.gather(*(get(pl) for pl in cfg.catalog_playlists))
    return enrich(con, cfg, api, playlists, guides, dry_run=dry_run)
