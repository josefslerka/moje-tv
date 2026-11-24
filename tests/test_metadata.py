import json

import pytest

from tvserver import catalog, db, enrichment, m3u
from tvserver.config import Config
from tvserver.metadata import MetadataIndex, normalize


@pytest.fixture
def api():
    return {
        "channels": [
            {"id": "Example.au", "name": "Example TV", "alt_names": ["Example Television"], "country": "AU", "categories": ["news"]},
            {"id": "Example.nz", "name": "Example TV", "country": "NZ", "categories": ["general"]},
            {"id": "Rare.au", "name": "Rare Channel", "country": "AU", "categories": ["documentary"]},
            {"id": "Plus.au", "name": "Example+", "country": "AU", "categories": ["movies"]},
            {"id": "Closed.au", "name": "Old TV", "country": "AU", "closed": "2020-01-01"},
            {"id": "Blocked.au", "name": "Blocked TV", "country": "AU"},
        ],
        "streams": [{"channel": "Example.au", "feed": "Sydney", "url": "https://cdn.test/sydney.m3u8"}],
        "feeds": [{"channel": "Example.au", "id": "National", "name": "National", "is_main": True, "languages": ["eng"]},
                  {"channel": "Example.au", "id": "Sydney", "name": "Sydney", "alt_names": ["NSW"], "languages": ["cze"]},
                  {"channel": "Rare.au", "id": "Main", "name": "Main", "is_main": True, "languages": ["eng"]}],
        "logos": [{"channel": "Example.au", "feed": "Sydney", "url": "https://logos.test/sydney.png"}],
        "blocklist": [{"channel": "Blocked.au"}],
        "countries": [{"code": "AU", "name": "Australia"}, {"code": "NZ", "name": "New Zealand"}],
        "languages": [{"code": "eng", "name": "English"}, {"code": "ces", "name": "Czech"}], "categories": [],
    }


def test_matches_id_url_alias_and_feed_without_fuzzy_guessing(api):
    index = MetadataIndex(api)
    assert index.resolve("example.au@Sydney", "Other label", "").tvg_id == "Example.au@Sydney"
    assert index.resolve("Example-au", "Other label", "").method == "normalized_id"
    url = index.resolve("old-id", "Unknown", "https://cdn.test/sydney.m3u8", "NZ", True)
    assert url.tvg_id == "Example.au@Sydney" and url.method == "url"
    alias = index.resolve("mjh-custom", "Example Television Ⓖ (1080p)", "", "AU", True)
    assert alias.tvg_id == "Example.au" and alias.method == "alias"
    assert index.resolve("", "Example TV NSW", "", "AU", True).tvg_id == "Example.au@Sydney"
    assert index.resolve("", "Example Television HD", "", "AU", True).tvg_id == "Example.au"
    assert index.resolve("", "Example TV Extra", "").method == "unmatched"
    assert normalize("Example+") != normalize("Example")


def test_ambiguous_and_conflicting_names_are_not_assigned(api):
    index = MetadataIndex(api)
    ambiguous = index.resolve("custom", "Example TV", "")
    assert ambiguous.channel is None and ambiguous.method == "ambiguous"
    assert ambiguous.candidates == ("Example.au", "Example.nz")
    assert index.resolve("custom", "Example TV", "", "NZ", True).tvg_id == "Example.nz"
    assert index.resolve("custom", "Example TV", "", "US", True).method == "country_conflict"
    # Výchozí země smí pomoci rozlišit název, nesmí popřít jedinečnou stanici z jiné země.
    assert index.resolve("", "Rare Channel", "", "NZ", False).tvg_id == "Rare.au"
    assert index.resolve("", "Old TV", "").channel is None
    assert index.resolve("", "Blocked TV", "").channel is None
    api["streams"].append({"channel": "Example.nz", "url": "https://cdn.test/sydney.m3u8"})
    assert MetadataIndex(api).resolve("", "Unknown", "https://cdn.test/sydney.m3u8").method == "ambiguous"


def test_playlist_enrichment_uses_the_matched_feed_and_explicit_attributes(api):
    entries = [m3u.Entry(name="Unlabelled", tvg_id="mjh-x", url="https://cdn.test/sydney.m3u8"),
               m3u.Entry(name="Unknown", url="https://cdn.test/unknown.m3u8", group="news",
                         attrs={"tvg-country": "NZ", "tvg-language": "English;ces"})]
    rows = catalog.build_playlist(entries, "List", Config(), api, [{"channel": "Example.au"}], country="NZ")
    assert (rows[0]["tvg_id"], rows[0]["country"], rows[0]["categories"], rows[0]["languages"], rows[0]["has_epg"]) == (
        "Example.au@Sydney", "AU", "news", "ces", 1)
    assert rows[0]["logo"] == "https://logos.test/sydney.png"
    assert (rows[1]["tvg_id"], rows[1]["country"], rows[1]["categories"], rows[1]["languages"]) == ("", "NZ", "news", "eng,ces")


def test_logos_confirm_names_but_cannot_identify_a_channel_alone(api):
    index = MetadataIndex(api)
    logo = "https://logos.test/sydney.png"
    match = index.resolve("", "Example TV", "", logo=logo)
    assert match.tvg_id == "Example.au" and match.method == "name+logo"
    assert index.resolve("", "Rare Channel", "", logo=logo).method == "logo_conflict"
    assert index.resolve("", "No matching name", "", logo=logo).channel is None


def test_country_hint_can_describe_broadcast_market_instead_of_origin(api):
    api["feeds"][0]["broadcast_area"] = ["c/US", "s/AU-NSW"]
    index = MetadataIndex(api)
    match = index.resolve("", "Example TV", "", "US", True)
    assert match.tvg_id == "Example.au" and match.channel["country"] == "AU"


def test_existing_catalogue_enrichment_is_reviewable_and_idempotent(tmp_path, api):
    cfg = Config(database=tmp_path / "data" / "tv.db", catalog_playlists=[{"name": "List", "country": "NZ"}])
    con = db.connect(cfg.database)
    try:
        entries = [m3u.Entry(name="Unlabelled", tvg_id="mjh-x", url="https://cdn.test/sydney.m3u8", user_agent="UA"),
                   m3u.Entry(name="Example TV", tvg_id="custom", url="https://cdn.test/ambiguous.m3u8"),
                   m3u.Entry(name="Not in metadata", url="https://cdn.test/unknown.m3u8", group="news",
                             attrs={"tvg-country": "NZ", "tvg-language": "English"})]
        rows = [{"url": e.url, "source": "List", "tvg_id": e.tvg_id, "name": e.name, "country": "",
                 "categories": "", "languages": "", "logo": "", "quality": "1080p", "labels": "label",
                 "user_agent": e.user_agent, "referrer": "https://ref.test/", "has_epg": 0, "restream": 0, "grp": "My group"}
                for e in entries]
        catalog._store(con, "List", rows)
        db.import_entries(con, [entries[0]])
        con.execute("UPDATE channels SET name='My own name', grp='My own group', favorite=1, last_ok=?", ("2026-11-05T08:00:00+00:00",))
        con.commit()
        playlist = [({"name": "List"}, entries)]
        before = [tuple(row) for row in con.execute("SELECT * FROM catalog ORDER BY url")]
        report = tmp_path / "preview.json"
        preview = enrichment.enrich(con, cfg, api, playlist, [], dry_run=True, report_path=report)
        assert preview["identified"] == 1 and preview["ambiguous"] == 1 and preview["unmatched_after"] == 2
        assert [tuple(row) for row in con.execute("SELECT * FROM catalog ORDER BY url")] == before
        evidence = json.loads(report.read_text())["items"]
        assert next(row for row in evidence if row["name"] == "Unlabelled")["method"] == "url"

        applied = enrichment.enrich(con, cfg, api, playlist, [], report_path=tmp_path / "applied.json")
        assert applied["channels_updated"] == 1
        row = catalog.lookup(con, entries[0].url)
        assert (row["tvg_id"], row["country"], row["categories"], row["languages"]) == ("Example.au@Sydney", "AU", "news", "ces")
        assert (row["source"], row["grp"], row["user_agent"], row["referrer"], row["quality"], row["labels"]) == (
            "List", "My group", "UA", "https://ref.test/", "1080p", "label")
        own = db.all_channels(con)[0]
        assert (own["name"], own["grp"], own["favorite"], own["last_ok"]) == (
            "My own name", "My own group", 1, "2026-11-05T08:00:00+00:00")
        assert own["tvg_id"] == "Example.au@Sydney"
        unknown = catalog.lookup(con, entries[2].url)
        assert (unknown["tvg_id"], unknown["country"], unknown["categories"], unknown["languages"]) == ("", "NZ", "news", "eng")
        again = enrichment.enrich(con, cfg, api, playlist, [], report_path=tmp_path / "again.json")
        assert again["enriched"] == 0
        rebuilt = catalog.build_playlist(entries, "List", cfg, api, [])
        assert rebuilt[0]["tvg_id"] == row["tvg_id"] and rebuilt[0]["categories"] == row["categories"]
    finally:
        con.close()
