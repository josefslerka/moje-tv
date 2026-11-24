import json

from fastapi.testclient import TestClient
import pytest

from tvserver import db, m3u
from tvserver.app import create_app
from tvserver.config import Config
from tvserver.recordings import RecordingService, RESERVE_BYTES


@pytest.fixture
def app(tmp_path):
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"ffmpeg": str(tmp_path / "ffmpeg")}))
    (tmp_path / "ffmpeg").touch()
    cfg = Config(database=tmp_path / "tv.db", base_dir=tmp_path, caption_runtime=runtime)
    app = create_app(cfg, scheduler=False)
    db.import_entries(app.state.con, [m3u.Entry(name="CNN", url="https://stream.test/live.m3u8")])
    return app


@pytest.mark.parametrize(("mode", "translation", "provider"), [
    ("asr", None, "local"), ("translate-cs", "cs", "local"), ("translate-cs-api", "cs", "openrouter"),
])
def test_only_explicit_recording_choice_can_use_paid_translation(app, monkeypatch, mode, translation, provider):
    captions = app.state.captions
    monkeypatch.setattr(captions, "status", lambda: {"available": True, "translation": True, "cloud_translation": True})

    async def capture(service, active, ffmpeg, source):
        assert source.startswith("http://127.0.0.1:8090/proxy?")
        await active.stop.wait()
        service.captions.remove(active.caption_id)
        service._update(active.id, state="ready", finished_at=db.now())
        service.active = None

    monkeypatch.setattr(RecordingService, "_run", capture)
    with TestClient(app) as client:
        assert client.post("/api/recordings", json={"channel_id": 999}).status_code == 404
        assert client.post("/api/recordings", json={"channel_id": 1, "video": False, "text_mode": None}).status_code == 422
        item = client.post("/api/recordings", json={"channel_id": 1, "text_mode": mode}).json()
        sid = next(iter(captions.sessions))
        session = captions.sessions[sid]
        assert (session.language, session.translation, session.translation_provider) == ("auto", translation, provider)
        assert client.post("/api/recordings", json={"channel_id": 1}).status_code == 409
        assert client.delete("/api/recordings/" + item["id"]).status_code == 409
        assert len(client.get("/api/recordings").json()["recordings"]) == 1  # Survives a new viewer.
        assert client.post("/api/recordings/" + item["id"] + "/stop").status_code == 200
        assert client.post("/api/recordings/" + item["id"] + "/stop").status_code == 200
        assert not captions.sessions


def test_interrupted_recording_recovers_video_and_exports_timed_text(app):
    service = app.state.recordings
    rid = "a" * 32
    path = service.folder / rid
    path.mkdir(parents=True)
    (path / "video.part").write_bytes(b"playable-fragmented-mp4")
    (path / "segment-00000000.ts").write_bytes(b"temporary-audio")
    (path / "transcript.jsonl").write_text(
        json.dumps({"start": .25, "end": 1.75, "text": "Česká věta.", "source_text": "English sentence.",
                    "source_language": "en"}) + "\n" +
        json.dumps({"start": 2, "end": 3, "text": "<original>", "translation_failed": True,
                    "source_language": "fr"}) + '\n{"partial":')
    app.state.con.execute(
        "INSERT INTO recordings (id,channel_name,started_at,state,video,text_mode,max_seconds,duration) "
        "VALUES (?, 'CNN',?, 'recording',1,'translate-cs',7200,3)", (rid, db.now()))
    app.state.con.commit()
    with TestClient(app) as client:
        item = client.get("/api/recordings").json()["recordings"][0]
        assert item["state"] == "interrupted" and item["has_video"] and item["has_text"]
        assert not (path / "video.part").exists() and not list(path.glob("segment-*"))
        response = client.get(f"/api/recordings/{rid}/video", headers={"Range": "bytes=0-7"})
        assert response.status_code == 206 and response.content == b"playable"
        response = client.get(f"/api/recordings/{rid}/text.srt")
        assert "00:00:00,250 --> 00:00:01,750" in response.text
        assert "[FR] &lt;original&gt;" in response.text
        assert response.headers["content-disposition"].startswith("attachment")
        text = client.get(f"/api/recordings/{rid}/text.txt").text
        assert "[EN] English sentence." in text
        assert client.get(f"/api/recordings/{rid}/text.vtt").text.startswith("WEBVTT")
        assert client.delete(f"/api/recordings/{rid}").status_code == 204
        assert not path.exists()
        assert client.get(f"/api/recordings/{rid}/video").status_code == 404


def test_low_disk_space_rejects_start_before_creating_files_or_caption_session(app, monkeypatch):
    monkeypatch.setattr(RecordingService, "storage", lambda _: {"free_bytes": RESERVE_BYTES})
    with TestClient(app) as client:
        response = client.post("/api/recordings", json={"channel_id": 1})
        assert response.status_code == 409 and "1 GB" in response.json()["detail"]
        assert not app.state.recordings.list() and not app.state.captions.sessions
