import asyncio
import time

import httpx

from tvserver import checker, db, m3u
from tvserver.config import Config

SAMPLE = """#EXTM3U
#EXTINF:-1 tvg-id="Foo.us@SD" tvg-logo="http://x/l.png" group-title="News",Foo News (720p)
#EXTVLCOPT:http-user-agent=TestUA
#EXTVLCOPT:http-referrer=https://ref.example/
https://cdn.example.com/foo/index.m3u8
#EXTINF:-1 tvg-id="Bar.cz" group-title="Česko",Bar TV
http://stream.bar.cz/live.m3u8
"""


def test_parse_and_render_roundtrip():
    entries = m3u.parse(SAMPLE)
    assert [e.name for e in entries] == ["Foo News (720p)", "Bar TV"]
    foo = entries[0]
    assert (foo.tvg_id, foo.group, foo.user_agent, foo.referrer) == (
        "Foo.us@SD", "News", "TestUA", "https://ref.example/")
    again = m3u.parse(m3u.render(entries))
    assert again == entries


def test_import_is_idempotent(tmp_path):
    con = db.connect(tmp_path / "t.db")
    assert db.import_entries(con, m3u.parse(SAMPLE)) == (2, 0)
    assert db.import_entries(con, m3u.parse(SAMPLE)) == (0, 2)
    assert len(db.all_channels(con)) == 2


def test_blocklist():
    cfg = Config(blocked_hosts=["88.212.15.50", "usnlive.com"], block_ip_hosts=True)
    assert checker.is_allowed("https://cdn.example.com/a.m3u8", cfg)
    assert not checker.is_allowed("http://88.212.15.50/live/x.m3u8", cfg)
    assert not checker.is_allowed("https://s1.usnlive.com/stream/x.m3u8", cfg)
    assert not checker.is_allowed("http://23.237.104.106:8080/x.m3u8", cfg)
    assert checker.base_id("EbS.be@SD") == checker.base_id("ebs.be")


MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlow/index.m3u8\n"
MEDIA = "#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3,\nseg1.ts\n"


def _client(routes):
    def handler(req):
        body = routes.get(str(req.url))
        return httpx.Response(200, text=body) if body is not None else httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_check_stream_follows_master_playlist():
    async def go():
        async with _client({"https://a.test/master.m3u8": MASTER,
                            "https://a.test/low/index.m3u8": MEDIA}) as c:
            return await checker.check_stream(c, "https://a.test/master.m3u8")
    assert asyncio.run(go()) == (True, "HLS")


def test_check_stream_detects_dead_variant_and_404():
    async def go():
        async with _client({"https://a.test/master.m3u8": MASTER}) as c:
            return (await checker.check_stream(c, "https://a.test/master.m3u8"),
                    await checker.check_stream(c, "https://a.test/missing.m3u8"))
    assert asyncio.run(go()) == ((False, "varianta HTTP 404"), (False, "HTTP 404"))


def test_dead_channel_is_repaired_from_allowed_source(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    db.import_entries(con, [m3u.Entry(name="Foo", url="https://old.test/x.m3u8", tvg_id="Foo.us@SD")])
    cfg = Config(blocked_hosts=["bad.test"], hide_after_failures=2)
    clock = ["2026-11-05T08:00:00+00:00"]
    monkeypatch.setattr(db, "now", lambda: clock[0])

    async def fake_check(client, url, ua="", ref=""):
        if url == "https://new.test/x.m3u8":
            clock[0] = "2026-11-05T08:01:00+00:00"
        return (url == "https://new.test/x.m3u8"), "test"

    async def fake_candidates(client, cfg):
        return {"foo.us": [m3u.Entry(name="Foo", url="https://bad.test/x.m3u8", tvg_id="Foo.us"),
                           m3u.Entry(name="Foo", url="https://new.test/x.m3u8", tvg_id="Foo.us@HD")]}

    monkeypatch.setattr(checker, "check_stream", fake_check)
    monkeypatch.setattr(checker, "load_candidates", fake_candidates)
    s = asyncio.run(checker.run_check(con, cfg))
    assert s["repaired"] == 1
    row = db.all_channels(con)[0]
    assert row["url"] == "https://new.test/x.m3u8" and row["status"] == "ok"
    assert row["last_check"] == row["last_ok"] == "2026-11-05T08:01:00+00:00"


def test_channel_hidden_after_repeated_failures(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    db.import_entries(con, [m3u.Entry(name="Foo", url="https://old.test/x.m3u8")])
    cfg = Config(hide_after_failures=2)

    async def dead(client, url, ua="", ref=""):
        return False, "HTTP 403"

    monkeypatch.setattr(checker, "check_stream", dead)
    asyncio.run(checker.run_check(con, cfg, repair=False))
    assert len(db.visible(con, 2)) == 1
    asyncio.run(checker.run_check(con, cfg, repair=False))
    assert len(db.visible(con, 2)) == 0


def test_clean_name():
    assert m3u.clean_name("CT24 (1080p)") == "CT24"
    assert m3u.clean_name("RTM+ (720p) [Not 24/7]") == "RTM+"
    assert m3u.clean_name("Polar TV (Czech Republic) (1080p)") == "Polar TV (Czech Republic)"


def test_xmltv_time_and_plex_suffix_fallback(tmp_path):
    import gzip
    from datetime import datetime, timezone
    from tvserver import epg
    assert epg.parse_time("20261103050000 +0100") == datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)
    xml = """<?xml version="1.0"?><tv>
<channel id="NEW-abc"><display-name>A</display-name></channel>
<channel id="X"><display-name>B</display-name></channel>
<programme channel="NEW-abc" start="20261103050000 +0000" stop="20261103060000 +0000"><title>Zprávy</title></programme>
<programme channel="X" start="20261103050000 +0000" stop="20261103060000 +0000"><title>Jiné</title></programme>
</tv>"""
    f = tmp_path / "g.xml"
    f.write_bytes(gzip.compress(xml.encode()))
    since = datetime(2026, 11, 3, tzinfo=timezone.utc)
    until = datetime(2026, 11, 4, tzinfo=timezone.utc)
    res = epg.read_xmltv(f, {"OLD-abc", "X"}, since, until)
    assert [p[2] for p in res["OLD-abc"]] == ["Zprávy"]
    assert [p[2] for p in res["X"]] == ["Jiné"]


def test_resolve_prefers_manual_then_feed_order():
    from tvserver import epg
    cfg = Config(epg_mjh_feeds=["PlutoTV/us", "Plex/us"],
                 epg_map={"Man.cz": "https://e.test/cz.xml.gz#MAN.cz"})
    rows = [{"id": 1, "tvg_id": "Man.cz@SD"}, {"id": 2, "tvg_id": "Auto.us@SD"}, {"id": 3, "tvg_id": "None.us"}]
    guides = [{"channel": "Auto.us", "site": "i.mjh.nz", "site_id": "Plex/us#p1"},
              {"channel": "Auto.us", "site": "i.mjh.nz", "site_id": "PlutoTV/us#q1"},
              {"channel": "Man.cz", "site": "i.mjh.nz", "site_id": "Plex/us#zzz"}]
    m = epg.resolve(rows, cfg, guides)
    assert m[1] == ("https://e.test/cz.xml.gz", "MAN.cz", "manual")
    assert m[2] == ("https://i.mjh.nz/PlutoTV/us.xml.gz", "q1", "auto")
    assert 3 not in m


def _api():
    return {
        "channels": [
            {"id": "Good.us", "name": "Good News", "country": "US", "categories": ["news"], "is_nsfw": False, "closed": None},
            {"id": "Adult.us", "name": "Adult", "country": "US", "categories": [], "is_nsfw": True, "closed": None},
            {"id": "Closed.us", "name": "Closed", "country": "US", "categories": [], "is_nsfw": False, "closed": "2020-01-01"},
            {"id": "Blocked.us", "name": "Blocked", "country": "US", "categories": [], "is_nsfw": False, "closed": None},
        ],
        "streams": [
            {"channel": "Good.us", "feed": None, "url": "https://cdn.good.test/a.m3u8", "quality": "1080p", "labels": []},
            {"channel": "Good.us", "feed": None, "url": "https://cdn2.good.test/b.m3u8", "quality": "720p", "labels": []},
            {"channel": "Good.us", "feed": "Europe", "url": "https://cdn.good.test/eu.m3u8", "quality": "", "labels": []},
            {"channel": "Good.us", "feed": None, "url": "http://10.0.0.1/leak.m3u8", "quality": "", "labels": []},
            {"channel": "Adult.us", "feed": None, "url": "https://x.test/1.m3u8", "quality": "", "labels": []},
            {"channel": "Closed.us", "feed": None, "url": "https://x.test/2.m3u8", "quality": "", "labels": []},
            {"channel": "Blocked.us", "feed": None, "url": "https://x.test/3.m3u8", "quality": "", "labels": []},
            {"channel": None, "feed": None, "url": "https://x.test/4.m3u8", "quality": "", "labels": []},
        ],
        "feeds": [{"channel": "Good.us", "id": "SD", "name": "SD", "is_main": True, "languages": ["eng"]},
                  {"channel": "Good.us", "id": "Europe", "name": "Europe", "is_main": False, "languages": ["cze"]}],
        "logos": [{"channel": "Good.us", "feed": None, "in_use": True, "url": "https://l.test/g.png"}],
        "blocklist": [{"channel": "Blocked.us"}],
    }


def test_catalog_build_filters_and_search(tmp_path):
    from tvserver import catalog
    cfg = Config(block_ip_hosts=True)
    rows = catalog.build_iptv_org(_api(), [{"channel": "Good.us"}], cfg)
    assert sorted(r["url"] for r in rows) == ["https://cdn.good.test/a.m3u8", "https://cdn.good.test/eu.m3u8",
                                              "https://cdn2.good.test/b.m3u8", "https://x.test/1.m3u8"]
    eu = next(r for r in rows if r["url"].endswith("eu.m3u8"))
    assert (eu["name"], eu["tvg_id"], eu["languages"]) == ("Good News Europe", "Good.us@Europe", "ces")
    assert all(r["has_epg"] for r in rows if r["tvg_id"].startswith("Good.us"))
    flagged = next(r for r in rows if r["tvg_id"] == "Adult.us")
    assert flagged["has_epg"] == 0 and flagged["categories"] == "adult"

    con = db.connect(tmp_path / "t.db")
    catalog._store(con, catalog.IPTV_ORG, rows)
    res = catalog.search(con, q="good")
    assert res["total"] == 2                      # dva kanály: hlavní a evropská varianta
    main = next(i for i in res["items"] if i["key"] == "Good.us")
    assert len(main["streams"]) == 2 and main["logo"] == "https://l.test/g.png"
    assert catalog.search(con, language="ces")["total"] == 1

    cid = catalog.add_channel(con, "https://cdn.good.test/a.m3u8")
    assert catalog.search(con, q="good news")["items"][0]["channel_id"] == cid
    row = db.all_channels(con)[0]
    assert (row["name"], row["source"]) == ("Good News", "iptv-org")


def test_my_channels_api(tmp_path):
    from fastapi.testclient import TestClient
    from tvserver.app import create_app
    cfg = Config(database=tmp_path / "t.db", hide_after_failures=2)
    app = create_app(cfg, scheduler=False)
    con = app.state.con
    db.import_entries(con, [m3u.Entry(name="A", url="https://a.test/a.m3u8", group="Zprávy"),
                            m3u.Entry(name="B", url="https://b.test/b.m3u8", group="Zprávy"),
                            m3u.Entry(name="C", url="https://c.test/c.m3u8", group="Hudba")])
    ids = {r["name"]: r["id"] for r in db.all_channels(con)}
    with TestClient(app) as c:
        assert c.patch(f"/api/channels/{ids['C']}", json={"favorite": True}).status_code == 200
        assert [x["name"] for x in c.get("/api/channels").json()][0] == "C"          # oblíbené první
        assert c.patch(f"/api/channels/{ids['B']}", json={"enabled": False}).status_code == 200
        assert "B" not in [x["name"] for x in c.get("/api/channels").json()]
        assert c.patch(f"/api/channels/{ids['A']}", json={"name": " "}).status_code == 400
        assert c.patch(f"/api/channels/{ids['A']}", json={"epg": "nesmysl"}).status_code == 400
        order = [{"id": ids["C"], "group": "Zprávy"}, {"id": ids["A"], "group": "Nová"}, {"id": ids["B"], "group": "Nová"}]
        assert c.post("/api/channels/order", json={"items": order}).status_code == 200
        assert c.post("/api/groups/rename", json={"old": "Nová", "new": "Ostatní zprávy"}).json() == {"renamed": 2}
        full = c.get("/api/channels?all=true").json()
        assert [(x["name"], x["group"]) for x in full] == [("C", "Zprávy"), ("A", "Ostatní zprávy"), ("B", "Ostatní zprávy")]
        fav = c.get("/playlist.m3u?favorites=true").text
        assert fav.count("#EXTINF") == 1 and "c.test" in fav
        grp = c.get("/playlist.m3u", params={"group": "Ostatní zprávy"}).text
        assert grp.count("#EXTINF") == 1 and "a.test" in grp                         # B je vypnutý
        assert c.delete(f"/api/channels/{ids['A']}").status_code == 200
        assert len(c.get("/api/channels?all=true").json()) == 2


def test_proxy_signing_and_rewrite(tmp_path):
    from tvserver import proxy
    con = db.connect(tmp_path / "t.db")
    db.import_entries(con, [m3u.Entry(name="A", url="https://cdn.test/live/master.m3u8")])
    assert proxy.headers_for(con, "https://cdn.test/live/master.m3u8") is not None
    assert proxy.headers_for(con, "https://evil.test/x") is None
    text = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,URI="https://other.test/a.m3u8"\nlow/index.m3u8\n')
    out = proxy.rewrite(con, text, "https://cdn.test/live/master.m3u8")
    links = [l for l in out.splitlines() if l.startswith("/proxy")] + \
            [l.split('URI="')[1].split('"')[0] for l in out.splitlines() if 'URI="' in l]
    targets = []
    for ln in links:
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(ln).query)
        url, ok = proxy.decode(con, q["u"][0], q["s"][0])
        assert ok
        targets.append(url)
    assert sorted(targets) == ["https://cdn.test/live/key.bin", "https://cdn.test/live/low/index.m3u8",
                               "https://other.test/a.m3u8"]
    forged = proxy._b64("https://evil.test/x")
    assert proxy.decode(con, forged, "0" * 24) == ("https://evil.test/x", False)


def test_restreams_hidden_unless_requested(tmp_path):
    from tvserver import catalog
    api = _api()
    api["streams"].append({"channel": "Good.us", "feed": None, "url": "https://x.restream.test/good.m3u8",
                           "quality": "", "labels": []})
    cfg = Config(restream_hosts=["restream.test"])
    rows = catalog.build_iptv_org(api, [], cfg)
    assert {r["url"]: r["restream"] for r in rows}["https://x.restream.test/good.m3u8"] == 1
    con = db.connect(tmp_path / "t.db")
    catalog._store(con, catalog.IPTV_ORG, rows)
    main = lambda res: next(i for i in res["items"] if i["key"] == "Good.us")
    assert all(not s["restream"] for s in main(catalog.search(con, q="good"))["streams"])
    with_rs = main(catalog.search(con, q="good", restreams=True))
    assert len(with_rs["streams"]) == 3 and with_rs["streams"][-1]["restream"]   # oficiální první
    assert not with_rs["restream"]                                                 # kanál má i oficiální stream
    only = main(catalog.search(con, q="good", restreams_only=True))
    assert [s["restream"] for s in only["streams"]] == [True] and only["restream"]
    assert checker.is_restream("https://a.restream.test/x", cfg) and not checker.is_restream("https://good.test/x", cfg)


def test_source_filter_keeps_streams_of_that_source(tmp_path):
    from tvserver import catalog
    cfg = Config()
    con = db.connect(tmp_path / "t.db")
    catalog._store(con, catalog.IPTV_ORG, catalog.build_iptv_org(_api(), [], cfg), override=False)
    catalog._store(con, "Doporučené", [{"url": "https://cdn.good.test/a.m3u8", "source": "Doporučené",
        "tvg_id": "Good.us", "name": "Good News", "country": "US", "categories": "news", "languages": "eng",
        "logo": "", "quality": "", "labels": "", "user_agent": "", "referrer": "", "has_epg": 1, "restream": 0,
        "grp": "Zprávy"}])
    res = catalog.search(con, source="Doporučené")
    assert res["total"] == 1
    it = res["items"][0]
    assert [s["url"] for s in it["streams"]] == ["https://cdn.good.test/a.m3u8"] and it["group"] == "Zprávy"
    # obnova iptv-org nepřepíše adresu, která patří doporučeným
    catalog._store(con, catalog.IPTV_ORG, catalog.build_iptv_org(_api(), [], cfg), override=False)
    assert catalog.lookup(con, "https://cdn.good.test/a.m3u8")["source"] == "Doporučené"


FREETV = """#EXTM3U
#EXTINF:-1 tvg-id="Good.us" tvg-country="US" group-title="USA",Good News Ⓖ
https://free.test/good.m3u8
#EXTINF:-1 tvg-id="Nova.cz" tvg-country="CZ" group-title="Czech Republic",Nova Cinema
https://sktv.pirate.test/stream.php?ch=Nova
#EXTINF:-1 tvg-name="Film" group-title="VOD Movies (EN)",Film
https://free.test/film.mp4
#EXTINF:-1 tvg-id="mjh-x" group-title="Nz",Kiwi TV Ⓢ
https://free.test/kiwi.m3u8
#EXTINF:-1 group-title="News",Live News Ⓨ
https://www.youtube.com/@news/live
#EXTINF:-1 tvg-id="Good.us" group-title="USA",Good News
https://cdn.good.test/a.m3u8
"""


def _api_full():
    return _api() | {"countries": [{"code": "US", "name": "United States"}, {"code": "NZ", "name": "New Zealand"},
                                   {"code": "AU", "name": "Australia"}, {"code": "CZ", "name": "Czechia"}],
                     "categories": [], "languages": []}


def test_build_playlist_filters_and_enriches():
    from tvserver import catalog
    cfg = Config(blocked_hosts=["pirate.test"])
    rows = {r["url"]: r for r in catalog.build_playlist(m3u.parse(FREETV), "Free-TV", cfg, _api_full(),
                                                        [{"channel": "Good.us"}], ["VOD*"], "AU")}
    assert "https://sktv.pirate.test/stream.php?ch=Nova" not in rows and "https://free.test/film.mp4" not in rows
    good = rows["https://free.test/good.m3u8"]
    assert (good["name"], good["country"], good["categories"], good["languages"], good["labels"], good["has_epg"]) == (
        "Good News", "US", "news", "eng", "Geo-blocked", 1)
    assert (rows["https://free.test/kiwi.m3u8"]["name"], rows["https://free.test/kiwi.m3u8"]["country"]) == ("Kiwi TV", "NZ")
    yt = rows["https://www.youtube.com/@news/live"]
    assert (yt["country"], yt["categories"]) == ("AU", "news")


def test_playlist_import_does_not_exclude_nsfw_metadata():
    from tvserver import catalog
    entry = m3u.Entry(name="Flagged channel", tvg_id="Adult.us", url="https://free.test/flagged.m3u8")
    rows = catalog.build_playlist([entry], "Free-TV", Config(), _api_full(), [])
    assert len(rows) == 1
    assert (rows[0]["tvg_id"], rows[0]["url"]) == ("Adult.us", entry.url)
    assert rows[0]["categories"] == "adult"


def test_nsfw_category_preserves_original_tags_and_is_filterable(tmp_path):
    from tvserver import catalog
    api = _api_full()
    flagged = next(channel for channel in api["channels"] if channel["is_nsfw"])
    flagged["categories"] = ["news", "adult", "news"]
    rows = catalog.build_iptv_org(api, [], Config())
    assert next(row for row in rows if row["tvg_id"] == "Adult.us")["categories"] == "news,adult"
    entry = m3u.Entry(name="Flagged channel", tvg_id="Adult.us", url="https://free.test/flagged.m3u8")
    assert catalog.build_playlist([entry], "Free-TV", Config(), api, [])[0]["categories"] == "news,adult"
    assert flagged["categories"] == ["news", "adult", "news"]

    con = db.connect(tmp_path / "t.db")
    try:
        catalog._store(con, catalog.IPTV_ORG, rows)
        # Starší databáze nemusí mít novou syntetickou kategorii v katalogu názvů.
        db.set_meta(con, "catalog_names", '{"countries":{},"categories":{"news":"Zprávy"},"languages":{}}')
        result = catalog.search(con, category="adult")
        assert result["total"] == 1
        assert result["items"][0]["categories"] == ["Zprávy", "Pro dospělé"]
        facet = next(row for row in catalog.facets(con)["categories"] if row["id"] == "adult")
        assert facet == {"id": "adult", "name": "Pro dospělé", "count": 1}
    finally:
        con.close()


def test_catalog_refresh_with_extra_playlists(tmp_path, monkeypatch):
    import json
    from tvserver import catalog
    api = _api_full()
    def handler(req):
        u = str(req.url)
        if u.startswith("https://api.test/"):
            return httpx.Response(200, json=api.get(u.rsplit("/", 1)[1][:-5], []))
        if u == "https://lists.test/free.m3u8":
            return httpx.Response(200, text=FREETV)
        return httpx.Response(500)
    real = httpx.AsyncClient
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    async def no_guides(client, cfg):
        return []
    monkeypatch.setattr(catalog, "load_guides", no_guides)
    cfg = Config(catalog_api="https://api.test", blocked_hosts=["pirate.test"], catalog_playlists=[
        {"name": "Free-TV", "url": "https://lists.test/free.m3u8", "skip_groups": ["VOD*"]},
        {"name": "Rozbitý", "url": "https://lists.test/broken.m3u8"}])
    con = db.connect(tmp_path / "t.db")
    s = asyncio.run(catalog.refresh(con, cfg))
    assert s["lists"] == {"Free-TV": 3}       # adresu cdn.good.test/a.m3u8 už má iptv-org
    assert catalog.lookup(con, "https://cdn.good.test/a.m3u8")["source"] == "iptv-org"
    assert catalog.lookup(con, "https://free.test/good.m3u8")["source"] == "Free-TV"
    # druhá obnova dá stejný výsledek, seznam vyřazený z konfigurace zmizí
    assert asyncio.run(catalog.refresh(con, cfg))["lists"] == {"Free-TV": 3}
    cfg.catalog_playlists = []
    asyncio.run(catalog.refresh(con, cfg))
    assert catalog.lookup(con, "https://free.test/good.m3u8") is None
    assert json.loads(db.get_meta(con, "catalog_playlists")) == []


def test_web_streams(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tvserver import webstream
    from tvserver.app import create_app
    assert webstream.is_web("https://www.youtube.com/@WION/live") and webstream.is_web("https://www.twitch.tv/x")
    assert not webstream.is_web("https://cdn.example.com/youtube.com.m3u8")
    assert 4 * 3600 < webstream._ttl("https://m.test/api/manifest/hls_variant/expire/%d/ei/x" % (time.time() + 6 * 3600)) <= 5 * 3600
    assert webstream._ttl("https://m.test/x.m3u8") == webstream.DEFAULT_TTL

    async def fake(url):
        if "offline" in url:
            raise RuntimeError("vysílání neběží")
        return "https://manifest.test/master.m3u8"
    monkeypatch.setattr(webstream, "resolve", fake)
    async def go():
        async with _client({"https://manifest.test/master.m3u8": MEDIA}) as c:
            return (await checker.check_stream(c, "https://www.youtube.com/@a/live"),
                    await checker.check_stream(c, "https://www.youtube.com/@offline/live"))
    ok, dead = asyncio.run(go())
    assert ok == (True, "HLS") and dead == (False, "živé vysílání: vysílání neběží")

    cfg = Config(database=tmp_path / "t.db")
    app = create_app(cfg, scheduler=False)
    db.import_entries(app.state.con, [m3u.Entry(name="YT", url="https://www.youtube.com/@a/live", group="Zprávy")])
    with TestClient(app) as c:
        pl = c.get("/playlist.m3u").text
        assert "/live?u=https%3A%2F%2Fwww.youtube.com%2F%40a%2Flive" in pl
        r = c.get("/live", params={"u": "https://www.youtube.com/@a/live"}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "https://manifest.test/master.m3u8"
        assert c.get("/live", params={"u": "https://www.youtube.com/@jiny/live"}).status_code == 404
        assert c.get("/api/channels").json()[0]["web"] is True


# --- wiki a Wikidata -------------------------------------------------------------------

def test_wikidata_domain_and_classify():
    from tvserver import wikidata as w
    assert w.domain("https://www.france24.com/en/") == "france24.com"
    assert w.domain("https://www.abc.net.au/news/") == "abc.net.au"
    assert w.domain("https://bbc.co.uk") == "bbc.co.uk"
    ch = lambda name, types: {"name": name, "types": types}
    # výslovně státní vlastník vyhraje i nad „public“
    assert w.classify("public broadcaster", [ch("Všeruská státní televizní společnost", "broadcaster")]) == "státní"
    # německá zemská stanice je veřejnoprávní, i když řetězec končí u spolkové země
    assert w.classify("television station", [ch("Westdeutscher Rundfunk", "german public state broadcaster"),
                                              ch("Severní Porýní-Vestfálsko", "federated state")]) == "veřejnoprávní"
    # řetězec končící u státu
    assert w.classify("television station", [ch("France Médias Monde", "organization"),
                                              ch("Francie", "country | sovereign state")]) == "státní"
    # mezivládní organizace není vláda
    assert w.classify("tv", [ch("OSN", "intergovernmental organization | international organization")]) == "neznámé"
    assert w.classify("television station", [ch("Bloomberg L.P.", "company | business")]) == "soukromé"


def test_wiki_notes_preserved_and_gone_pages(tmp_path):
    from tvserver import wiki
    p = tmp_path / "Kanál.md"
    head = '---\ntyp: kanál\nwikidata: "Q42"\nwikidata_overeno: true\nvlastnictvi_rucne: "soukromé"\n---\n'
    p.write_text(head + "# Kanál\n\n" + wiki.notes_block("Moje poznámka\n- bod"), encoding="utf-8")
    notes, keys = wiki.read_existing(p)
    assert notes.startswith("Moje poznámka") and "- bod" in notes
    assert keys == {"wikidata": "Q42", "wikidata_overeno": True, "vlastnictvi_rucne": "soukromé"}
    assert wiki.safe_name('CBS News 24/7: "live"') == "CBS News 24 7 live"
    cfg = Config(restream_hosts=["restream.test"])
    assert wiki.platforms("https://jmp2.uk/plu-1.m3u8", cfg) == ["Pluto TV"]
    assert wiki.platforms("https://lds-wonder-samsungau.amagi.tv/x.m3u8", cfg) == ["Samsung TV Plus", "Amagi"]
    assert wiki.platforms("https://x.restream.test/a.m3u8", cfg) == ["Restreamy"]
    assert wiki.platforms("https://live.france24.com/a.m3u8", cfg) == ["Vlastní server stanice"]


def test_wiki_build_end_to_end(tmp_path, monkeypatch):
    import asyncio
    from tvserver import catalog, wiki
    cfg = Config(database=tmp_path / "data" / "tv.db", wiki_dir=tmp_path / "vault" / "appletv" / "wiki",
                 obsidian_prefix="appletv/wiki")
    con = db.connect(cfg.database)
    catalog._store(con, catalog.IPTV_ORG, catalog.build_iptv_org(_api(), [{"channel": "Good.us"}], cfg), override=False)
    catalog.add_channel(con, "https://cdn.good.test/a.m3u8", group="Zprávy")

    async def iptv(cfg):
        return {"good.us": {"name": "Good News", "owners": ["Good Corp Inc"], "website": "https://good.test",
                            "country": "US", "categories": ["news"]}}
    monkeypatch.setattr(wiki, "load_iptv_channels", iptv)
    monkeypatch.setattr(wiki, "lan_addresses", lambda port: {"ip_url": "http://10.0.0.5:8090", "local_url": ""})

    async def fake_enrich(con, items, overrides):
        return {it["key"]: {"match": "jistá", "qid": "Q1", "label": "Good News", "owners": [],
                            "ownership": "neznámé", "summary": {"lang": "en", "title": "Good News",
                            "extract": "Good News is a channel.", "url": "https://en.wikipedia.org/wiki/Good_News"}}
                for it in items}
    monkeypatch.setattr(wiki.wikidata, "enrich", fake_enrich)

    s1 = asyncio.run(wiki.build(con, cfg))
    page = cfg.wiki_dir / "Kanály" / "Good News.md"
    text = page.read_text(encoding="utf-8")
    assert "Good News is a channel." in text
    assert 'vlastnictvi: "soukromé"' in text   # vlastník podle iptv-org je firma
    assert "http://10.0.0.5:8090/?ch=" in text
    page.write_text(text + "Moje poznámka.\n", encoding="utf-8")
    s2 = asyncio.run(wiki.build(con, cfg))
    assert "Moje poznámka." in page.read_text(encoding="utf-8")
    assert s2["changed"] == 0 and s1["pages"] == 1
    assert wiki.check_links(cfg.wiki_dir, cfg.obsidian_prefix) == []
    # kanál zmizí z výběru: stránka zůstane s upozorněním
    con.execute("DELETE FROM channels"); con.commit()
    s3 = asyncio.run(wiki.build(con, cfg))
    assert s3["gone"] == 1 and wiki.GONE_MARK in page.read_text(encoding="utf-8")
    assert "Moje poznámka." in page.read_text(encoding="utf-8")


def test_wikiweb_render(tmp_path):
    from tvserver import wikiweb
    (tmp_path / "Kanály").mkdir()
    (tmp_path / "Zdroje").mkdir()
    (tmp_path / "Zdroje" / "CDN.md").write_text("# CDN\n\n## Co to je\n", encoding="utf-8")
    (tmp_path / "Kanály" / "X.md").write_text(
        '---\ntyp: kanál\nv_mem_seznamu: true\n---\n\n# X\n\n> [!info] Stav\n> Hraje\n\n'
        "| a | b |\n|---|---|\n| [[p/Zdroje/CDN\\|CDN]] | [[p/Zdroje/CDN#Co to je|kotva]] |\n\n"
        "[Přehrát](http://10.0.0.5:8090/?ch=1) [[jinde/Stránka]]\n", encoding="utf-8")
    r = wikiweb.render(tmp_path, "Kanály/X", "p", "vault", 8090)
    assert r["title"] == "X" and r["meta"]["v_mem_seznamu"] is True
    assert '<blockquote class="callout callout-info"><p class="callout-title">Stav</p><p>Hraje' in r["html"]
    assert 'href="/wiki/Zdroje/CDN">CDN</a></td>' in r["html"]          # odkaz v tabulce nerozbije sloupce
    assert 'href="/wiki/Zdroje/CDN#co-to-je"' in r["html"]
    assert 'href="/?ch=1"' in r["html"]
    assert 'href="obsidian://open?vault=vault&amp;file=jinde/Str%C3%A1nka"' in r["html"]
    assert wikiweb.render(tmp_path, "../X", "p", "vault", 8090) is None   # mimo složku wiki ne
    assert [h["title"] for h in wikiweb.search(tmp_path, "hraje")] == ["X"]
    assert [(g["folder"], g["pages"][0]["mine"]) for g in wikiweb.tree(tmp_path)] == [("Zdroje", False), ("Kanály", True)]
