import asyncio

import httpx
import pytest

from tvserver import catalog, db, m3u
from tvserver.config import Config
from tvserver.metadata import MetadataIndex


def _api():
    return {
        "channels": [
            {"id": "News.us", "name": "Example News", "alt_names": ["Example Television"],
             "country": "US", "categories": ["news"]},
            {"id": "News.ca", "name": "Example News", "country": "CA", "categories": ["general"]},
            {"id": "Closed.us", "name": "Closed TV", "closed": "2020-01-01"},
            {"id": "Blocked.us", "name": "Blocked TV"},
        ],
        "feeds": [{"channel": "News.us", "id": "International", "name": "International",
                   "languages": ["eng"]}],
        "logos": [{"channel": "News.us", "feed": "International", "url": "https://logo.test/news.png"}],
        "streams": [], "blocklist": [{"channel": "Blocked.us"}],
        "countries": [], "categories": [], "languages": [],
    }


@pytest.mark.parametrize("group, expected", [
    ("Zpravodajství", "news"), ("Informativos", "news"), ("National News", "news"),
    ("Sporty", "sports"), ("Deportivos Int.", "sports"), ("Documentaries", "documentary"),
    ("Business News", "business,news"), ("Science & Nature", "science,outdoor"),
    ("Kids & Family", "kids,family"), ("Comedy", "comedy"),
    ("Andalucía", ""), ("Español", ""),
])
def test_playlist_genres_from_additional_sources(group, expected):
    entry = m3u.Entry(name="Unmapped station", url="https://cdn.test/unmapped.m3u8", group=group)
    row, = catalog.build_playlist([entry], "Extra", Config(), _api(), [])
    assert row["categories"] == expected


def test_playlist_genre_does_not_replace_matched_station_metadata():
    entry = m3u.Entry(name="Example News", tvg_id="News.us", url="https://cdn.test/news.m3u8", group="Infantiles")
    row, = catalog.build_playlist([entry], "Extra", Config(), _api(), [])
    assert row["categories"] == "news"


def test_api_streams_without_station_metadata_remain_searchable(tmp_path):
    api = _api()
    api["streams"] = [
        {"channel": None, "title": "Example News International", "url": "https://cdn.test/news.m3u8",
         "quality": "1080p", "labels": ["Geo-blocked"], "user_agent": "TestUA", "referrer": "https://ref.test/"},
        {"channel": None, "title": "Unknown TV (720p)", "url": "https://cdn.test/unknown.m3u8"},
        {"channel": "OldID.us", "title": "Example Television", "url": "https://cdn.test/alias.m3u8"},
        {"channel": "UnknownID.us", "title": "Another unknown TV", "url": "https://cdn.test/old-id.m3u8"},
        {"channel": None, "title": "Example News", "url": "https://cdn.test/ambiguous.m3u8"},
        {"channel": None, "url": "https://cdn.test/no-name.m3u8"},
    ]
    rows = catalog.build_iptv_org(api, [{"channel": "News.us"}], Config())
    assert len(rows) == 5
    news = rows[0]
    assert (news["tvg_id"], news["country"], news["categories"], news["languages"], news["has_epg"]) == (
        "News.us@International", "US", "news", "eng", 1)
    assert (news["quality"], news["labels"], news["user_agent"], news["referrer"], news["logo"]) == (
        "1080p", "Geo-blocked", "TestUA", "https://ref.test/", "https://logo.test/news.png")
    assert rows[2]["tvg_id"] == "News.us"
    assert rows[3]["tvg_id"] == "UnknownID.us"
    for row in (rows[1], rows[4]):
        assert (row["tvg_id"], row["country"], row["categories"], row["languages"], row["has_epg"]) == ("", "", "", "", 0)
    assert rows[4]["_metadata_method"] == "ambiguous"
    con = db.connect(tmp_path / "tv.db")
    try:
        catalog._store(con, catalog.IPTV_ORG, rows)
        result = catalog.search(con, q="unknown")
        assert result["total"] == 2
        assert catalog.search(con, category="news")["total"] == 2
        assert con.execute("SELECT COUNT(*) FROM stream_checks").fetchone()[0] == 0
    finally:
        con.close()


def test_missing_metadata_cannot_bypass_source_exclusions():
    api = _api()
    api["blocklist"].append({"channel": "MissingBlocked.us"})
    api["streams"] = [
        {"channel": "MissingBlocked.us", "title": "Unnamed TV", "url": "https://cdn.test/blocked-id.m3u8"},
        {"channel": None, "title": "Blocked TV", "url": "https://cdn.test/blocked-name.m3u8"},
        {"channel": None, "title": "Closed TV", "url": "https://cdn.test/closed-name.m3u8"},
        {"channel": None, "title": "Unknown TV", "url": "https://blocked.test/a.m3u8"},
        {"channel": None, "title": "Unknown TV", "url": "http://192.0.2.1/a.m3u8"},
        {"channel": None, "title": "Unknown TV", "url": "https://cdn.test/live/user/pass/123.ts"},
        {"channel": None, "title": "Unknown TV", "url": "https://user:pass@cdn.test/a.m3u8"},
    ]
    assert catalog.build_iptv_org(api, [], Config(blocked_hosts=["blocked.test"])) == []


def test_playlist_url_hints_respect_authoritative_api_and_ambiguity():
    api = _api()
    api["streams"] = [{"channel": "News.ca", "url": "https://cdn.test/authoritative.m3u8"}]
    entries = [m3u.Entry(name="Unlabelled", url=url, tvg_id=tvg_id) for url, tvg_id in (
        ("https://cdn.test/authoritative.m3u8", "News.us@International"),
        ("https://cdn.test/extra.m3u8", "News.us@International"),
        ("https://cdn.test/conflict.m3u8", "News.us"),
        ("https://cdn.test/conflict.m3u8", "News.ca"))]
    index = MetadataIndex(api, entries)
    assert index.resolve("old-id", "Unknown", entries[0].url).tvg_id == "News.ca"
    inferred = index.resolve("old-id", "Unknown", entries[1].url)
    assert (inferred.tvg_id, inferred.method) == ("News.us@International", "playlist_url")
    assert index.resolve("Closed.us", "Old TV", entries[1].url).tvg_id == "News.us@International"
    assert index.resolve("", "Unknown", entries[2].url).method == "ambiguous"
    compatible_name = index.resolve("", "Example Television", entries[2].url)
    assert (compatible_name.tvg_id, compatible_name.method) == ("News.us", "alias+url")
    api["blocklist"].append({"channel": "Closed.us"})
    rows = catalog.build_playlist([m3u.Entry(name="Old TV", tvg_id="Closed.us", url=entries[1].url)],
                                  "Extra", Config(), api, [], metadata_index=MetadataIndex(api, entries))
    assert rows == []


def test_m3u_preserves_commas_in_names_and_quoted_attributes():
    text = '#EXTM3U\n#EXTINF:-1 tvg-id="TV.us" group-title="News, Culture",TV, International "HD"\nhttps://cdn.test/tv.m3u8\n'
    entry, = m3u.parse_catalog(text)
    assert entry.name == 'TV, International "HD"'
    assert entry.group == "News, Culture" and entry.tvg_id == "TV.us"
    assert m3u.parse(m3u.render([entry])) == [entry]
    assert m3u.parse_catalog("\ufeff  \n" + text) == [entry]


def test_unique_name_in_one_playlist_repairs_an_old_id_in_another():
    api = _api()
    first = m3u.Entry(name="Different name", url="https://cdn.test/shared.m3u8", tvg_id="OldMissingID.us")
    second = m3u.Entry(name="Example Television", url=first.url, tvg_id="CustomID")
    lists = [({"name": "First"}, [first]), ({"name": "Second"}, [second])]
    index = catalog.playlist_metadata_index(api, lists, Config(), [])
    row, = catalog.build_playlist([first], "First", Config(), api, [], metadata_index=index)
    assert (row["tvg_id"], row["categories"], row["country"]) == ("News.us", "news", "US")
    assert row["_metadata_method"] == "playlist_url"




def test_refresh_keeps_last_known_metadata_until_station_identity_changes(tmp_path, monkeypatch):
    cfg = Config(database=tmp_path / "tv.db")
    api = _api()
    api["channels"][0]["categories"] = []
    api["streams"] = [{"channel": "News.us", "url": "https://cdn.test/news.m3u8"}]

    async def load_api(client, cfg):
        return api

    async def load_guides(client, cfg):
        return []

    monkeypatch.setattr(catalog, "load_api", load_api)
    monkeypatch.setattr(catalog, "load_guides", load_guides)
    con = db.connect(cfg.database)
    try:
        old = catalog.build_iptv_org(api, [], cfg)[0]
        old.update(categories="news", languages="eng", logo="https://logo.test/remembered.png")
        catalog._store(con, catalog.IPTV_ORG, [old])
        con.commit()
        asyncio.run(catalog.refresh(con, cfg))
        row = catalog.lookup(con, old["url"])
        assert (row["categories"], row["languages"], row["logo"]) == ("news", "eng", old["logo"])
        api["streams"][0]["channel"] = "News.ca"
        asyncio.run(catalog.refresh(con, cfg))
        changed = catalog.lookup(con, old["url"])
        assert (changed["tvg_id"], changed["categories"], changed["languages"], changed["logo"]) == ("News.ca", "general", "", "")
    finally:
        con.close()


@pytest.mark.parametrize("body", ["<html>Temporarily unavailable</html>", "#EXTM3U\n",
    "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,Segment\nhttps://cdn.test/segment.ts\n"])
def test_refresh_keeps_previous_source_when_http_200_is_not_a_channel_list(tmp_path, monkeypatch, body):
    cfg = Config(database=tmp_path / "tv.db", catalog_playlists=[{"name": "Extra", "url": "https://list.test/tv.m3u"}])
    con = db.connect(cfg.database)
    api = _api()
    original = catalog.build_playlist([m3u.Entry(name="Old TV", url="https://cdn.test/old.m3u8")], "Extra", cfg, api, [])
    catalog._store(con, "Extra", original)
    con.commit()
    before = [tuple(row) for row in con.execute("SELECT * FROM catalog")]
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport, **kwargs))

    async def load_api(client, cfg):
        return api

    async def load_guides(client, cfg):
        return []

    monkeypatch.setattr(catalog, "load_api", load_api)
    monkeypatch.setattr(catalog, "load_guides", load_guides)
    try:
        result = asyncio.run(catalog.refresh(con, cfg))
        assert result["lists"] == {}
        assert [tuple(row) for row in con.execute("SELECT * FROM catalog")] == before
        with pytest.raises(ValueError):
            asyncio.run(catalog.import_playlist(con, cfg, "https://list.test/tv.m3u"))
        assert [tuple(row) for row in con.execute("SELECT * FROM catalog")] == before
    finally:
        con.close()
