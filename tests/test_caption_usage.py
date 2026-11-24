import asyncio
from datetime import datetime
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from tvserver import db, m3u
from tvserver.app import create_app
from tvserver.caption_cloud import MODEL, OpenRouterTranslator
from tvserver.caption_translation import TranslationRejected
from tvserver.caption_usage import CaptionUsage
from tvserver.config import Config


def ledger(tmp_path):
    con = db.connect(tmp_path / "usage.db")
    db.import_entries(con, [m3u.Entry(name="CNN", url="https://example.test/live.m3u8")])
    return con, CaptionUsage(con)


def test_actual_costs_sum_exactly_and_survive_restart_without_double_counting(tmp_path):
    con, usage = ledger(tmp_path)
    since = usage.summary()["since"]
    for value in [.000000123, .000000456, 0]:
        request_id = usage.start(MODEL, 1)
        usage.finish(request_id, status="success", cost_usd=value, input_tokens=30, output_tokens=10)
        usage.finish(request_id, status="success", cost_usd=999)  # A repeated callback cannot replace a settlement.
    total = usage.summary()["total"]
    assert total["known_cost_usd"] == .000000579
    assert total["requests"] == total["known_cost_requests"] == 3
    assert total["input_tokens"] == 90 and total["output_tokens"] == 30
    con.close()
    con = db.connect(tmp_path / "usage.db")
    reopened = CaptionUsage(con)
    assert reopened.summary()["total"] == total
    assert reopened.summary()["since"] == since
    con.close()


def test_unknown_cost_and_pending_requests_are_not_treated_as_free(tmp_path):
    con, usage = ledger(tmp_path)
    known_zero = usage.start(MODEL, 1)
    usage.finish(known_zero, status="success", cost_usd=0)
    unknown = usage.start(MODEL, 1)
    usage.finish(unknown, status="cancelled")
    pending = usage.start(MODEL, 1)
    total = usage.summary()["total"]
    assert total["requests"] == 3 and total["known_cost_requests"] == 1
    assert total["unknown_cost_requests"] == 1 and total["pending_requests"] == 1
    CaptionUsage(con)  # Simulate server restart with a request still in flight.
    total = usage.summary()["total"]
    assert total["unknown_cost_requests"] == 2 and total["pending_requests"] == 0
    assert con.execute("SELECT status FROM caption_api_usage WHERE id=?", (pending,)).fetchone()[0] == "interrupted"
    con.close()


@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf"), True, "bad-price"])
def test_missing_or_invalid_costs_remain_unknown(tmp_path, value):
    con, usage = ledger(tmp_path)
    request_id = usage.start(MODEL, 1)
    usage.finish(request_id, status="success", cost_usd=value)
    total = usage.summary()["total"]
    assert total["known_cost_requests"] == 0 and total["unknown_cost_requests"] == 1
    assert con.execute("SELECT cost_nanos FROM caption_api_usage").fetchone()[0] is None
    con.close()


@pytest.mark.parametrize("at,month_start,today_start", [
    ("2026-11-06T21:00:00+00:00", "2026-10-31T23:00:00+00:00", "2026-11-05T23:00:00+00:00"),
    ("2026-03-30T05:00:00+00:00", "2026-02-28T23:00:00+00:00", "2026-03-29T22:00:00+00:00"),
])
def test_today_and_month_use_prague_calendar_including_daylight_saving(tmp_path, monkeypatch, at, month_start, today_start):
    from datetime import timedelta

    con, usage = ledger(tmp_path)
    for boundary in [month_start, today_start]:
        instant = datetime.fromisoformat(boundary)
        for stamp in [instant - timedelta(microseconds=1), instant]:
            monkeypatch.setattr(db, "now", lambda stamp=stamp: stamp.isoformat(timespec="microseconds"))
            usage.finish(usage.start(MODEL, 1), status="success", cost_usd=.01)
    report = usage.summary(datetime.fromisoformat(at))
    assert report["today"]["requests"] == 1 and report["today"]["known_cost_usd"] == .01
    assert report["month"]["requests"] == 3 and report["month"]["known_cost_usd"] == .03
    assert report["total"]["requests"] == 4 and report["total"]["known_cost_usd"] == .04
    con.close()


def test_channel_history_survives_channel_removal(tmp_path):
    con, usage = ledger(tmp_path)
    usage.finish(usage.start(MODEL, 1), status="success", cost_usd=.000012)
    con.execute("DELETE FROM channels WHERE id=1")
    con.commit()
    row = usage.summary()["by_channel"][0]
    assert row["channel_name"] == "CNN" and row["known_cost_usd"] == .000012
    assert row["requests"] == 1
    con.close()


def test_future_periods_do_not_leak_into_today_or_this_month(tmp_path, monkeypatch):
    con, usage = ledger(tmp_path)
    for stamp in ["2026-11-06T10:00:00+00:00", "2026-11-07T10:00:00+00:00", "2026-12-01T10:00:00+00:00"]:
        monkeypatch.setattr(db, "now", lambda stamp=stamp: datetime.fromisoformat(stamp).isoformat(timespec="microseconds"))
        usage.finish(usage.start(MODEL, 1), status="success", cost_usd=.01)
    report = usage.summary(datetime.fromisoformat("2026-11-06T22:00:00+00:00"))
    assert report["today"]["requests"] == 1 and report["month"]["requests"] == 2
    assert report["total"]["requests"] == 3
    con.close()


def runtime(tmp_path):
    env = tmp_path / "key.env"
    env.write_text("OPENROUTER_API_KEY=sk-or-v1-" + "x" * 40 + "\n")
    return {"openrouter": {"model": MODEL, "env_file": str(env)}}


@pytest.mark.parametrize("outcome", ["success", "empty", "truncated", "http_error"])
def test_api_reported_cost_is_recorded_even_when_translation_fails(tmp_path, outcome):
    con, usage = ledger(tmp_path)

    def respond(request):
        return httpx.Response(503 if outcome == "http_error" else 200, json={
            "id": "gen-public-test", "choices": [{"finish_reason": "length" if outcome == "truncated" else "stop",
                "message": {"content": None if outcome == "empty" else "Český výsledek"}}],
            "usage": {"cost": .000012, "prompt_tokens": 32, "completion_tokens": 8}})

    async def run():
        translator = OpenRouterTranslator(runtime(tmp_path), recorder=usage,
                                          client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        try:
            if outcome == "success":
                await translator.translate("PRIVATE_CAPTION", channel_id=1)
            else:
                with pytest.raises((RuntimeError, TranslationRejected)):
                    await translator.translate("PRIVATE_CAPTION", channel_id=1)
        finally:
            await translator.close()
    asyncio.run(run())
    total = usage.summary()["total"]
    assert total["requests"] == total["known_cost_requests"] == 1
    assert total["known_cost_usd"] == .000012
    assert total["input_tokens"] == 32 and total["output_tokens"] == 8
    row = dict(con.execute("SELECT * FROM caption_api_usage").fetchone())
    assert row["generation_id"] == "gen-public-test"
    assert row["status"] == {"success": "success", "empty": "rejected", "truncated": "rejected", "http_error": "http_error"}[outcome]
    assert "PRIVATE_CAPTION" not in json.dumps(row) and "sk-or-" not in json.dumps(row)
    con.close()


def test_cancellation_records_unknown_cost_even_when_no_cue_can_be_returned(tmp_path):
    con, usage = ledger(tmp_path)

    async def run():
        started = asyncio.Event()

        async def respond(request):
            started.set()
            await asyncio.Event().wait()

        translator = OpenRouterTranslator(runtime(tmp_path), recorder=usage,
                                          client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        task = asyncio.create_task(translator.translate("PRIVATE_CAPTION", channel_id=1))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await translator.close()
    asyncio.run(run())
    total = usage.summary()["total"]
    assert total["requests"] == total["unknown_cost_requests"] == 1
    assert total["pending_requests"] == total["known_cost_requests"] == 0
    assert con.execute("SELECT status FROM caption_api_usage").fetchone()[0] == "cancelled"
    con.close()


def test_silence_and_oversized_text_create_no_billing_records(tmp_path):
    con, usage = ledger(tmp_path)

    async def run():
        translator = OpenRouterTranslator(runtime(tmp_path), recorder=usage)
        await translator.translate(" ", channel_id=1)
        with pytest.raises(TranslationRejected):
            await translator.translate("x" * 1601, channel_id=1)
        assert translator.client is None
        await translator.close()
    asyncio.run(run())
    assert usage.summary()["total"]["requests"] == 0
    con.close()


def test_admin_api_exposes_same_persistent_caption_totals_in_settings(tmp_path):
    cfg = Config(database=tmp_path / "app.db", wiki_dir=tmp_path / "wiki", base_dir=tmp_path)
    app = create_app(cfg, scheduler=False)
    with TestClient(app) as client:
        assert client.get("/api/captions/usage").json()["total"]["requests"] == 0
        recorder = app.state.captions.usage
        recorder.finish(recorder.start(MODEL), status="success", cost_usd=.000012)
        response = client.get("/api/captions/usage")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["total"]["known_cost_usd"] == .000012
        assert client.get("/api/settings").json()["caption_usage"]["total"] == response.json()["total"]
