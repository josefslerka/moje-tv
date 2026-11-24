import asyncio

import pytest
from fastapi.testclient import TestClient

from tvserver import catalog, checker, db, m3u
from tvserver.app import create_app
from tvserver.config import Config


FIRST = "2026-11-05T08:00:00+00:00"
SECOND = "2026-11-05T08:01:00+00:00"
THIRD = "2026-11-05T08:02:00+00:00"


@pytest.fixture
def clock(monkeypatch):
    current = [FIRST]
    monkeypatch.setattr(db, "now", lambda: current[0])
    return current


@pytest.fixture
def cfg(tmp_path):
    return Config(database=tmp_path / "t.db", wiki_dir=tmp_path / "wiki")


@pytest.mark.parametrize("preview", [None, "/api/resolve", "/api/test"])
def test_add_keeps_time_of_actual_verification(cfg, clock, monkeypatch, preview):
    url = "https://stream.test/live.m3u8"
    checked = []

    async def good(client, url, ua="", ref=""):
        checked.append(url)
        return True, "HLS"

    monkeypatch.setattr(catalog, "check_stream", good)
    with TestClient(create_app(cfg, scheduler=False)) as client:
        if preview:
            payload = {"urls": [url]} if preview.endswith("resolve") else {"url": url}
            assert client.post(preview, json=payload).status_code == 200
            clock[0] = SECOND
        response = client.post("/api/channels", json={"url": url, "name": "TV", "skip_test": bool(preview)})
        assert response.status_code == 200
        row = client.get("/api/channels?all=true").json()[0]
        assert (row["status"], row["last_check"], row["last_ok"], row["detail"], row["fail_count"]) == (
            "ok", FIRST, FIRST, "HLS", 0)
    assert checked == [url]


def test_unchecked_add_does_not_claim_success(cfg, clock):
    with TestClient(create_app(cfg, scheduler=False)) as client:
        assert client.post("/api/channels", json={
            "url": "https://stream.test/unknown.m3u8", "name": "TV", "skip_test": True,
        }).status_code == 200
        row = client.get("/api/channels?all=true").json()[0]
        assert (row["status"], row["last_check"], row["last_ok"], row["detail"]) == ("unknown", None, None, "")


@pytest.mark.parametrize("endpoint", ["/api/test", "/api/resolve"])
def test_individual_checks_keep_last_success_on_failure(cfg, clock, monkeypatch, endpoint):
    url = "https://stream.test/live.m3u8"
    result = [True, "HLS"]

    async def check(client, url, ua="", ref=""):
        return tuple(result)

    monkeypatch.setattr(catalog, "check_stream", check)
    app = create_app(cfg, scheduler=False)
    with TestClient(app) as client:
        client.post("/api/channels", json={"url": url, "name": "TV", "skip_test": True})
        payload = {"urls": [url]} if endpoint.endswith("resolve") else {"url": url}

        def run_check():
            assert client.post(endpoint, json=payload).status_code == 200
            return client.get("/api/channels?all=true").json()[0]

        row = run_check()
        assert (row["last_check"], row["last_ok"]) == (FIRST, FIRST)
        clock[0] = SECOND
        result[:] = [False, "HTTP 403"]
        row = run_check()
        assert (row["status"], row["last_check"], row["last_ok"], row["fail_count"]) == ("dead", SECOND, FIRST, 1)
        assert client.post("/api/channels", json={"url": url, "name": "TV", "skip_test": True}).status_code == 200
        row = client.get("/api/channels?all=true").json()[0]
        assert (row["status"], row["last_check"], row["last_ok"], row["fail_count"]) == ("dead", SECOND, FIRST, 1)
        clock[0] = THIRD
        result[:] = [True, "HLS"]
        row = run_check()
        assert (row["status"], row["last_check"], row["last_ok"], row["fail_count"]) == ("ok", THIRD, THIRD, 0)
        # Jednotlivý test nesmí odložit pravidelnou kontrolu všech kanálů.
        assert db.get_meta(app.state.con, "last_check") is None


def test_preview_verification_survives_server_restart(cfg, clock, monkeypatch):
    url = "https://stream.test/live.m3u8"

    async def good(client, url, ua="", ref=""):
        return True, "HLS"

    monkeypatch.setattr(catalog, "check_stream", good)
    with TestClient(create_app(cfg, scheduler=False)) as client:
        assert client.post("/api/resolve", json={"urls": [url]}).status_code == 200
    clock[0] = SECOND
    with TestClient(create_app(cfg, scheduler=False)) as client:
        assert client.post("/api/channels", json={"url": url, "name": "TV", "skip_test": True}).status_code == 200
        row = client.get("/api/channels?all=true").json()[0]
        assert (row["last_check"], row["last_ok"]) == (FIRST, FIRST)


@pytest.mark.parametrize("previous_success", [False, True])
def test_failed_preview_keeps_real_history_when_added(cfg, clock, monkeypatch, previous_success):
    url = "https://stream.test/live.m3u8"
    result = [previous_success, "test"]

    async def check(client, url, ua="", ref=""):
        return tuple(result)

    monkeypatch.setattr(catalog, "check_stream", check)
    with TestClient(create_app(cfg, scheduler=False)) as client:
        if previous_success:
            client.post("/api/test", json={"url": url})
        clock[0] = SECOND
        result[:] = [False, "HTTP 403"]
        client.post("/api/test", json={"url": url})
        clock[0] = THIRD
        assert client.post("/api/channels", json={"url": url, "name": "TV", "skip_test": True}).status_code == 200
        row = client.get("/api/channels?all=true").json()[0]
        assert (row["status"], row["last_check"], row["last_ok"], row["detail"]) == (
            "dead", SECOND, FIRST if previous_success else None, "HTTP 403")


def test_existing_timestamps_are_kept_when_upgrading_database(cfg, clock):
    url = "https://stream.test/live.m3u8"
    con = db.connect(cfg.database)
    db.import_entries(con, [m3u.Entry(name="TV", url=url)])
    con.execute("UPDATE channels SET status='dead', last_check=?, last_ok=?, detail='HTTP 403'", (SECOND, FIRST))
    con.execute("DROP TABLE stream_checks")  # Databáze vytvořená před zavedením evidence podle URL.
    con.commit()
    con.close()
    con = db.connect(cfg.database)
    try:
        con.execute("DELETE FROM channels")
        con.commit()
        catalog.add_channel(con, url, name="TV")
        row = db.all_channels(con)[0]
        assert (row["status"], row["last_check"], row["last_ok"], row["detail"]) == ("dead", SECOND, FIRST, "HTTP 403")
    finally:
        con.close()


def test_batch_uses_each_completion_time_and_preserves_last_success(cfg, clock, monkeypatch):
    con = db.connect(cfg.database)
    try:
        db.import_entries(con, [m3u.Entry(name="A", url="https://a.test/live.m3u8"),
                                m3u.Entry(name="B", url="https://b.test/live.m3u8")])
        con.execute("UPDATE channels SET last_ok=? WHERE name='B'", (FIRST,))
        con.commit()

        async def check(client, url, ua="", ref=""):
            clock[0] = SECOND if url.startswith("https://b.") else FIRST
            return url.startswith("https://a."), "test"

        async def candidates(client, cfg):
            clock[0] = THIRD
            return {}

        monkeypatch.setattr(checker, "check_stream", check)
        monkeypatch.setattr(checker, "load_candidates", candidates)
        result = asyncio.run(checker.run_check(con, cfg))
        assert (result["ok"], result["dead"]) == (1, 1)
        a, b = db.all_channels(con)
        assert (a["last_check"], a["last_ok"]) == (FIRST, FIRST)
        assert (b["last_check"], b["last_ok"], b["fail_count"]) == (SECOND, FIRST, 1)
        assert db.get_meta(con, "last_check") == THIRD
    finally:
        con.close()


def test_imported_url_change_does_not_reuse_old_verification(cfg, clock, monkeypatch):
    con = db.connect(cfg.database)
    try:
        original = "https://a.test/live.m3u8"
        replacement = "https://b.test/live.m3u8"
        db.import_entries(con, [m3u.Entry(name="TV", url=original, tvg_id="TV.cz")])

        async def good(client, url, ua="", ref=""):
            return True, "HLS"

        monkeypatch.setattr(catalog, "check_stream", good)
        asyncio.run(catalog.test(con, original))
        assert db.all_channels(con)[0]["last_ok"] == FIRST
        db.import_entries(con, [m3u.Entry(name="TV", url=replacement, tvg_id="TV.cz")])
        row = db.all_channels(con)[0]
        assert (row["status"], row["last_check"], row["last_ok"], row["detail"]) == ("unknown", None, None, "")
    finally:
        con.close()


def test_late_result_does_not_overwrite_a_more_recent_check(cfg, clock):
    con = db.connect(cfg.database)
    try:
        url = "https://stream.test/live.m3u8"
        db.import_entries(con, [m3u.Entry(name="TV", url=url)])
        db.record_check(con, url, False, "HTTP 403", SECOND)
        db.record_check(con, url, True, "HLS", FIRST)
        con.commit()
        row = db.all_channels(con)[0]
        assert (row["status"], row["last_check"], row["last_ok"], row["detail"], row["fail_count"]) == (
            "dead", SECOND, FIRST, "HTTP 403", 1)
    finally:
        con.close()
