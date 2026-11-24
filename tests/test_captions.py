import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from tvserver.app import create_app
from tvserver.captions import CaptionBusy, CaptionService, Job, JobCancelled
from tvserver.caption_cloud import CloudTranslation
from tvserver.config import Config
from tvserver import db, m3u


class FakeStdin:
    def __init__(self, process):
        self.process = process

    def write(self, data):
        message = json.loads(data)
        self.process.messages.append(message)
        if self.process.respond:
            translated = message.get("translation") == "cs"
            for event in [
                {"type": "cue", "job": message["id"], "start": message["start"],
                 "end": message["start"] + 2, "text": "Český překlad" if translated else "English caption",
                 "language": "cs" if translated else message["language"],
                 "translation": message.get("translation") or False},
                {"type": "done", "job": message["id"]},
            ]:
                self.process.stdout.feed_data((json.dumps(event) + "\n").encode())

    async def drain(self):
        pass


class FakeWorker:
    def __init__(self, respond=True):
        self.stdout = asyncio.StreamReader()
        self.messages = []
        self.respond = respond
        self.stdin = FakeStdin(self)
        self.returncode = None


def fake_worker(monkeypatch, respond=True):
    workers, stopped = [], []

    async def start(service):
        if not service.process:
            service.process = FakeWorker(respond)
            workers.append(service.process)

    async def stop(service):
        if service.process:
            stopped.append(service.process)
            service.process.returncode = 0
            service.process = None

    monkeypatch.setattr(CaptionService, "_start_worker", start)
    monkeypatch.setattr(CaptionService, "_stop_worker", stop)
    monkeypatch.setattr(CaptionService, "status", lambda self: {
        "available": True, "language": "en", "languages": ["en", "cs"], "translation": True,
        "cloud_translation": True,
        "chunk_seconds": 2, "running": bool(self.process),
    })
    return workers, stopped


@pytest.fixture
def app(tmp_path):
    cfg = Config(database=tmp_path / "tv.db", wiki_dir=tmp_path / "wiki", base_dir=tmp_path)
    app = create_app(cfg, scheduler=False)
    db.import_entries(app.state.con, [m3u.Entry(name="CNN", url="https://stream.test/live.m3u8")])
    return app


def test_no_runtime_reports_unavailable_without_starting_model(app):
    with TestClient(app) as client:
        status = client.get("/api/captions/status").json()
        assert not status["available"]
        assert status["language"] == "en" and status["translation"] is False
        assert client.post("/api/captions/sessions", json={"channel_id": 1}).status_code == 503
        assert app.state.captions.process is None


def test_sessions_only_for_existing_enabled_channels(app, monkeypatch):
    fake_worker(monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/captions/sessions", json={"channel_id": 999}).status_code == 404
        app.state.con.execute("UPDATE channels SET enabled = 0 WHERE id = 1")
        app.state.con.commit()
        assert client.post("/api/captions/sessions", json={"channel_id": 1}).status_code == 404


@pytest.mark.parametrize("language", ["en", "cs"])
def test_audio_returns_incremental_cues_with_media_timestamps(app, monkeypatch, language):
    workers, _ = fake_worker(monkeypatch)
    with TestClient(app) as client:
        created = client.post("/api/captions/sessions", json={"channel_id": 1, "language": language}).json()
        sid = created["session"]
        assert created["translation"] is False
        assert created["language"] == language
        assert app.state.captions.process is None  # Creating a session does not load a model.
        response = client.post(f"/api/captions/sessions/{sid}/audio?start=123.5&end=129.5", content=b"mp4")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[0]["text"] == "English caption"
        assert (events[0]["start"], events[0]["end"]) == (123.5, 125.5)
        assert workers[0].messages[0]["session"] == sid
        assert workers[0].messages[0]["language"] == events[0]["language"] == language
        assert client.delete(f"/api/captions/sessions/{sid}").status_code == 204
        assert client.post(f"/api/captions/sessions/{sid}/audio?start=0&end=2", content=b"mp4").status_code == 404


def test_default_english_and_czech_sessions_keep_their_languages_separate(app, monkeypatch):
    workers, _ = fake_worker(monkeypatch)
    with TestClient(app) as client:
        english = client.post("/api/captions/sessions", json={"channel_id": 1}).json()
        czech = client.post("/api/captions/sessions", json={"channel_id": 1, "language": "cs"}).json()
        assert english["language"] == "en" and czech["language"] == "cs"
        assert english["session"] != czech["session"]
        for created in (english, czech):
            sid = created["session"]
            assert client.post(f"/api/captions/sessions/{sid}/audio?start=0&end=2", content=b"mp4").status_code == 200
        assert len(workers) == 1
        assert [message["language"] for message in workers[0].messages] == ["en", "cs"]


def test_unsupported_language_is_rejected_before_loading_model(app, monkeypatch):
    workers, _ = fake_worker(monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/captions/sessions", json={"channel_id": 1, "language": "fr"}).status_code == 422
        assert not app.state.captions.sessions and not workers


def test_translation_is_explicit_and_separate_from_forced_czech_and_plain_asr(app, monkeypatch):
    workers, _ = fake_worker(monkeypatch)
    with TestClient(app) as client:
        plain = client.post("/api/captions/sessions", json={"channel_id": 1}).json()
        translated = client.post("/api/captions/sessions", json={"channel_id": 1, "translation": "cs"}).json()
        forced = client.post("/api/captions/sessions", json={"channel_id": 1, "language": "cs"}).json()
        assert plain["translation"] is False and forced["translation"] is False
        assert translated["language"] == "en" and translated["translation"] == "cs"
        assert not workers  # Session creation remains lazy, including translation.
        for session in [plain, translated, forced]:
            response = client.post(f"/api/captions/sessions/{session['session']}/audio?start=15&end=17", content=b"mp4")
            event = json.loads(response.text.splitlines()[0])
            assert (event["start"], event["end"]) == (15, 17)
            assert event["translation"] == session["translation"]
        assert [m["translation"] for m in workers[0].messages] == [None, "cs", None]
        assert [m["language"] for m in workers[0].messages] == ["en", "en", "cs"]


def test_auto_language_request_is_preserved_until_worker_detection(app, monkeypatch):
    workers, _ = fake_worker(monkeypatch)
    with TestClient(app) as client:
        created = client.post("/api/captions/sessions", json={"channel_id": 1, "language": "auto", "translation": "cs"}).json()
        assert created["language"] == "auto" and created["translation"] == "cs"
        assert not workers
        response = client.post(f"/api/captions/sessions/{created['session']}/audio?start=15&end=17", content=b"mp4")
        assert response.status_code == 200
        assert workers[0].messages[0]["language"] == "auto"


@pytest.mark.parametrize("body", [{"translation": "fr"}, {"language": "cs", "translation": "cs"}])
def test_invalid_translation_rejected_before_creating_worker(app, monkeypatch, body):
    workers, _ = fake_worker(monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/captions/sessions", json={"channel_id": 1, **body}).status_code == 422
        assert not workers and not app.state.captions.sessions


def test_missing_translator_does_not_disable_english_sessions(app, monkeypatch):
    workers, _ = fake_worker(monkeypatch)
    monkeypatch.setattr(CaptionService, "status", lambda self: {"available": True, "translation": False})
    with TestClient(app) as client:
        assert client.post("/api/captions/sessions", json={"channel_id": 1, "translation": "cs"}).status_code == 503
        assert client.post("/api/captions/sessions", json={"channel_id": 1}).status_code == 200
        assert not workers


@pytest.mark.parametrize("times", ["start=-1&end=2", "start=4&end=2", "start=0&end=31", "start=nan&end=2"])
def test_audio_rejects_invalid_timestamps(app, monkeypatch, times):
    workers, _ = fake_worker(monkeypatch)
    with TestClient(app) as client:
        sid = client.post("/api/captions/sessions", json={"channel_id": 1}).json()["session"]
        assert client.post(f"/api/captions/sessions/{sid}/audio?{times}", content=b"mp4").status_code == 422
        assert not workers


def test_audio_size_limit_before_starting_worker(app, monkeypatch):
    from tvserver import captions
    fake_worker(monkeypatch)
    monkeypatch.setattr(captions, "MAX_AUDIO_BYTES", 4)
    with TestClient(app) as client:
        sid = client.post("/api/captions/sessions", json={"channel_id": 1}).json()["session"]
        assert client.post(f"/api/captions/sessions/{sid}/audio?start=0&end=2", content=b"12345").status_code == 413
        assert app.state.captions.process is None


def test_cancelling_session_interrupts_worker_and_does_not_leak_to_next_session(monkeypatch, tmp_path):
    workers, stopped = fake_worker(monkeypatch, respond=False)

    async def run():
        service = CaptionService(tmp_path / "unused.json")
        try:
            first = service.create(1)
            job = service.submit(first, 0, 2, b"first")
            async def consume():
                return [event async for event in service.events(job)]
            consuming = asyncio.create_task(consume())
            for _ in range(100):
                if workers and workers[0].messages:
                    break
                await asyncio.sleep(.001)
            with pytest.raises(CaptionBusy):
                service.submit(first, 2, 4, b"duplicate")
            service.remove(first)
            assert await asyncio.wait_for(consuming, 1) == []
            assert stopped == [workers[0]]
            second = service.create(2)
            next_job = service.submit(second, 80, 82, b"second")
            for _ in range(100):
                if len(workers) > 1 and workers[1].messages:
                    break
                await asyncio.sleep(.001)
            message = workers[1].messages[0]
            workers[1].stdout.feed_data((json.dumps({"type": "done", "job": message["id"]}) + "\n").encode())
            assert [event async for event in service.events(next_job)] == []
            assert message["session"] == second and message["start"] == 80
        finally:
            await service.close()
    asyncio.run(run())


class FakeCloud:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def translate(self, text, **context):
        self.calls.append(text)
        return CloudTranslation("Překlad přes API", .00001)

    async def close(self):
        self.closed = True


def test_paid_session_is_explicit_and_worker_only_transcribes_locally(app, monkeypatch):
    workers, _ = fake_worker(monkeypatch)
    cloud = FakeCloud()
    app.state.captions.cloud_translator = cloud
    with TestClient(app) as client:
        created = client.post("/api/captions/sessions", json={
            "channel_id": 1, "translation": "cs", "translation_provider": "openrouter"}).json()
        assert created["paid"] and created["translation_provider"] == "openrouter"
        assert not workers and not cloud.calls
        response = client.post(f"/api/captions/sessions/{created['session']}/audio?start=10&end=12", content=b"mp4")
        event = json.loads(response.text.splitlines()[0])
        assert event["text"] == "Překlad přes API"
        assert event["source_text"] == "English caption" and event["translation"] == "cs"
        assert event["translation_provider"] == "openrouter" and event["translation_api_called"]
        assert event["translation_cost_usd"] == .00001
        assert (event["start"], event["end"]) == (10, 12)
        assert workers[0].messages[0]["translation"] is None
        assert "key" not in workers[0].messages[0]
        assert cloud.calls == ["English caption"]
    assert cloud.closed


def test_free_subtitles_and_local_translation_never_call_paid_api(app, monkeypatch):
    fake_worker(monkeypatch)
    cloud = FakeCloud()
    app.state.captions.cloud_translator = cloud
    with TestClient(app) as client:
        for body in [{}, {"language": "auto"}, {"translation": "cs"}]:
            created = client.post("/api/captions/sessions", json={"channel_id": 1, **body}).json()
            assert not created["paid"]
            client.post(f"/api/captions/sessions/{created['session']}/audio?start=0&end=2", content=b"mp4")
        assert cloud.calls == []


@pytest.mark.parametrize("body", [{"translation_provider": "openrouter"},
                                  {"translation": "cs", "translation_provider": "unknown"}])
def test_paid_provider_requires_valid_explicit_translation(app, monkeypatch, body):
    workers, _ = fake_worker(monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/captions/sessions", json={"channel_id": 1, **body}).status_code == 422
        assert not workers and not app.state.captions.sessions


def test_missing_paid_configuration_does_not_disable_free_translation(app, monkeypatch):
    fake_worker(monkeypatch)
    monkeypatch.setattr(CaptionService, "status", lambda self: {
        "available": True, "translation": True, "cloud_translation": False})
    with TestClient(app) as client:
        assert client.post("/api/captions/sessions", json={"channel_id": 1, "translation": "cs",
                           "translation_provider": "openrouter"}).status_code == 503
        assert client.post("/api/captions/sessions", json={"channel_id": 1, "translation": "cs"}).status_code == 200


@pytest.mark.parametrize("language,text", [("cs", "Česká řeč"), ("en", ""), ("sv", "Svenska"), ("en", "x" * 1601)])
def test_paid_path_skips_czech_silence_unsupported_and_oversized_text(monkeypatch, tmp_path, language, text):
    fake_worker(monkeypatch)

    async def run():
        service = CaptionService(tmp_path / "unused.json")
        cloud = FakeCloud()
        service.cloud_translator = cloud
        sid = service.create(1, "auto", "cs", "openrouter")
        event = await service._cloud_cue(Job(sid, 0, 2, b"audio", translation="cs", translation_provider="openrouter"),
                                        {"type": "cue", "text": text, "language": language, "translation": False})
        assert not cloud.calls and not event["translation_api_called"]
        assert event["text"] == text
        await service.close()
    asyncio.run(run())


def test_paid_api_failure_keeps_original_and_retries_only_after_cooldown(monkeypatch, tmp_path):
    fake_worker(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr("tvserver.captions.time.monotonic", lambda: clock[0])

    class Flaky(FakeCloud):
        async def translate(self, text, **context):
            self.calls.append(text)
            if len(self.calls) == 1:
                raise RuntimeError("Private provider error")
            return CloudTranslation("Obnoveno")

    async def run():
        service = CaptionService(tmp_path / "unused.json")
        service.cloud_translator = Flaky()
        sid = service.create(1, "en", "cs", "openrouter")
        job = Job(sid, 0, 2, b"audio", translation="cs", translation_provider="openrouter")
        cue = {"type": "cue", "text": "Original", "language": "en", "translation": False}
        failed = await service._cloud_cue(job, cue)
        assert failed["text"] == "Original" and failed["translation_failed"]
        assert failed["translation_error"] == "api_error" and failed["translation_retry_seconds"] == 2
        waiting = await service._cloud_cue(job, cue)
        assert waiting["translation_error"] == "retry_wait" and not waiting["translation_api_called"]
        assert len(service.cloud_translator.calls) == 1
        clock[0] += 2
        assert (await service._cloud_cue(job, cue))["text"] == "Obnoveno"
        await service.close()
    asyncio.run(run())


def test_stopping_paid_session_cancels_request_and_never_processes_queued_paid_audio(monkeypatch, tmp_path):
    fake_worker(monkeypatch)

    async def run():
        started, cancelled = asyncio.Event(), asyncio.Event()

        class Blocking(FakeCloud):
            async def translate(self, text, **context):
                self.calls.append(text)
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        service = CaptionService(tmp_path / "unused.json")
        cloud = service.cloud_translator = Blocking()
        first = service.create(1, "en", "cs", "openrouter")
        second = service.create(2, "en", "cs", "openrouter")
        job = service.submit(first, 0, 2, b"first")
        queued = service.submit(second, 0, 2, b"second")

        async def consume():
            return [event async for event in service.events(job)]

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), 1)
        service.remove(first)
        service.remove(second)
        assert await asyncio.wait_for(consumer, 1) == []
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(service.jobs.join(), 1)
        assert cloud.calls == ["English caption"]
        assert [event async for event in service.events(queued)] == []
        await service.close()
    asyncio.run(run())
