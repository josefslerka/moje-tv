"""Konzervativní párování streamů s metadaty stanic a jejich vysílacích verzí."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re
import unicodedata

from .checker import base_id
from .m3u import Entry


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in value if char.isalnum() or char in "+&")


def clean_label(value: str) -> str:
    return re.sub(r"\(\d{3,4}p\)|\[(?:not 24/7|geo[- ]blocked|HD|SD|FHD|UHD)\]|[\u24b6-\u24cf]", "", value, flags=re.I).strip()


@dataclass(frozen=True)
class Match:
    tvg_id: str
    method: str
    channel: dict | None = None
    candidates: tuple[str, ...] = ()


class MetadataIndex:
    def __init__(self, api: dict[str, list], entries: list[Entry] = ()):
        self.channels = {row["id"].lower(): row for row in api["channels"]}
        self.blocked = {row["channel"].lower() for row in api.get("blocklist", [])}
        self.active = {key: row for key, row in self.channels.items() if not row.get("closed") and key not in self.blocked}
        self.ids: dict[str, set] = defaultdict(set)
        self.names: dict[str, set] = defaultdict(set)
        self.urls: dict[str, set] = defaultdict(set)
        self.logos: dict[str, set[str]] = defaultdict(set)
        self.markets: dict[str, set[str]] = defaultdict(set)
        self.feed_markets: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.rejected_names: dict[str, set[str]] = defaultdict(set)
        for key, row in self.channels.items():
            if key not in self.active:
                for label in [row["name"], *(row.get("alt_names") or [])]:
                    self.rejected_names[normalize(label)].add(row["id"])
        for key, row in self.active.items():
            self.ids[normalize(row["id"])].add((key, "", "normalized_id"))
            self.names[normalize(row["name"])].add((key, "", "name"))
            for alias in row.get("alt_names") or []:
                if normalize(alias):
                    self.names[normalize(alias)].add((key, "", "alias"))
        for feed in api.get("feeds", []):
            key = feed["channel"].lower()
            channel = self.active.get(key)
            if not channel:
                continue
            for area in feed.get("broadcast_area") or []:
                match = re.fullmatch(r"c/([A-Z]{2})|s/([A-Z]{2})-.+", area)
                if match:
                    country = match.group(1) or match.group(2)
                    self.markets[key].add(country)
                    self.feed_markets[(key, feed["id"])].add(country)
            channel_names = [channel["name"], *(channel.get("alt_names") or [])]
            for label in [feed.get("name") or feed["id"], *(feed.get("alt_names") or [])]:
                if not normalize(label):
                    continue
                value = (key, feed["id"], "feed_name")
                for name in channel_names:
                    self.names[normalize(name + " " + label)].add(value)
                    if normalize(label).startswith(normalize(name)) and normalize(label) != normalize(name):
                        self.names[normalize(label)].add(value)
        for logo in api.get("logos", []):
            key = logo["channel"].lower()
            if key in self.active and logo.get("url") and logo.get("in_use", True):
                self.logos[logo["url"]].add(key)
        for stream in api.get("streams", []):
            key = (stream.get("channel") or "").lower()
            if key in self.active and stream.get("url"):
                self.urls[stream["url"]].add((key, stream.get("feed") or "", "url"))
        # Známé ID uvedené přímo v jiném seznamu pomůže i řádku se stejnou URL,
        # který používá staré nebo neznámé ID. Neodvozujeme další shody z odhadů názvů.
        self.api_urls = set(self.urls)
        for entry in entries:
            key = base_id(entry.tvg_id)
            if key in self.active and entry.url not in self.api_urls:
                self.urls[entry.url].add((key, entry.tvg_id.partition("@")[2], "playlist_url"))

    def add_playlist_rows(self, rows: list[dict]) -> None:
        """Zpřístupní jednoznačně určené řádky dalším seznamům se stejnou URL."""
        for row in rows:
            key = base_id(row["tvg_id"])
            if row.get("_metadata_matched") and key in self.active and row["url"] not in self.api_urls:
                self.urls[row["url"]].add((key, row["tvg_id"].partition("@")[2], "playlist_url"))

    def _choose(self, values: set, original: str, country: str = "", strict_country: bool = False, logo: str = "") -> Match:
        confirmed_logo = False
        if logo in self.logos:
            supported = {value for value in values if value[0] in self.logos[logo]}
            if not supported:
                candidates = {self.channels[value[0]]["id"] for value in values} | {
                    self.channels[key]["id"] for key in self.logos[logo]}
                return Match(original, "logo_conflict", candidates=tuple(sorted(candidates)))
            values, confirmed_logo = supported, True
        if country:
            local = {value for value in values if self.channels[value[0]].get("country") == country}
            if not local:
                local = {value for value in values if country in (
                    self.feed_markets[(value[0], value[1])] if value[1] else self.markets[value[0]])}
            if local:
                values = local
            elif strict_country and not confirmed_logo and all(self.channels[value[0]].get("country") for value in values):
                return Match(original, "country_conflict", candidates=tuple(sorted({self.channels[v[0]]["id"] for v in values})))
        keys = {value[0] for value in values}
        if len(keys) != 1:
            return Match(original, "ambiguous", candidates=tuple(sorted(self.channels[key]["id"] for key in keys)))
        key = next(iter(keys))
        feeds = {value[1] for value in values}
        feed = next(iter(feeds)) if len(feeds) == 1 else ""
        channel = self.channels[key]
        tvg_id = channel["id"] + (f"@{feed}" if feed else "")
        method = min((value[2] for value in values), key=lambda value: (value != "name", value != "alias", value))
        if confirmed_logo:
            method += "+logo"
        return Match(tvg_id, method, channel)

    def resolve(self, tvg_id: str, name: str, url: str, country: str = "", strict_country: bool = False,
                alt_name: str = "", logo: str = "") -> Match:
        channel = self.channels.get(base_id(tvg_id)) if tvg_id else None
        if channel:
            # Staré ID zaniklé stanice nesmí zastínit přesnou URL aktuální stanice.
            if channel.get("closed") and base_id(tvg_id) not in self.blocked and self.urls.get(url):
                return self._choose(self.urls[url], tvg_id)
            _, sep, feed = tvg_id.partition("@")
            return Match(channel["id"] + (sep + feed if sep else ""), "id", channel)
        if tvg_id and self.ids.get(normalize(base_id(tvg_id))):
            match = self._choose(self.ids[normalize(base_id(tvg_id))], tvg_id)
            if match.channel and "@" in tvg_id:
                return Match(match.tvg_id + "@" + tvg_id.split("@", 1)[1], match.method, match.channel)
            return match
        url_match = None
        url_candidates = self.urls.get(url)
        if url_candidates:
            url_match = self._choose(url_candidates, tvg_id)
            if url_match.channel:
                return url_match
        for label in (name, alt_name):
            cleaned = clean_label(label)
            for variant in (cleaned, re.sub(r"\s+(?:HD|SD|FHD|UHD|4K)$", "", cleaned, flags=re.I)):
                key = normalize(variant)
                if key and self.names.get(key):
                    values = self.names[key]
                    if url_candidates:
                        # Spornou URL lze rozlišit názvem jen uvnitř jejích kandidátů.
                        keys = {value[0] for value in url_candidates}
                        values = {value for value in values if value[0] in keys}
                        if not values:
                            continue
                    match = self._choose(values, tvg_id, country, strict_country, logo)
                    if url_candidates and match.channel:
                        return Match(match.tvg_id, match.method + "+url", match.channel)
                    return match
                if key and self.rejected_names.get(key):
                    return Match(tvg_id, "excluded_by_source_rules", candidates=tuple(sorted(self.rejected_names[key])))
        return url_match or Match(tvg_id, "unmatched")
