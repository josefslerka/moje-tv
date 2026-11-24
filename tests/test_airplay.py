import json

from fastapi.testclient import TestClient
import pytest

from tvserver import db, m3u
from tvserver.airplay import AirPlayService, Segment, source_segments, webvtt
from tvserver.app import create_app
from tvserver.config import Config


@pytest.mark.parametrize(("mode", "translation", "provider"), [
    ("asr", None, "local"),
    ("translate-cs", "cs", "local"),
    ("translate-cs-api", "cs", "openrouter"),
])
def test_airplay_serves_sliding_hls_with_explicit_translation_provider(tmp_path, monkeypatch, mode, translation, provider):
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"ffmpeg": str(tmp_path / "ffmpeg")}))
    (tmp_path / "ffmpeg").touch()
    (tmp_path / "ffprobe").touch()
    cfg = Config(host="0.0.0.0", database=tmp_path / "tv.db", base_dir=tmp_path, caption_runtime=runtime)
    app = create_app(cfg, scheduler=False)
    db.import_entries(app.state.con, [m3u.Entry(name="CNN", url="https://stream.test/live.m3u8")])
    monkeypatch.setattr("tvserver.app.lan_addresses", lambda port: {"ip_url": f"http://192.0.2.1:{port}"})
    captions = app.state.captions
    monkeypatch.setattr(captions, "status", lambda: {"available": True, "translation": True, "cloud_translation": True})

    async def publish(service, session, ffmpeg, ffprobe, source):
        assert source.startswith(f"http://127.0.0.1:{cfg.port}/proxy?")
        for number in range(14):
            session.segments.append(Segment(number, 2, f"#EXT-X-PROGRAM-DATE-TIME:2026-11-07T10:00:{2 * number:02d}Z",
                                            b"video", "WEBVTT\n\n"))

    monkeypatch.setattr(AirPlayService, "_run", publish)
    with TestClient(app) as client:
        plain = client.post("/api/airplay/sessions", json={"channel_id": 1}).json()
        assert plain["session"] is None and plain["url"].startswith(f"http://192.0.2.1:{cfg.port}/proxy?")
        assert not captions.sessions
        assert client.post("/api/airplay/sessions", json={"channel_id": 2, "mode": mode}).status_code == 404

        created = client.post("/api/airplay/sessions", json={"channel_id": 1, "mode": mode}).json()
        status = f"/api/airplay/sessions/{created['session']}"
        prefix = f"/airplay/{created['session']}"
        assert client.get(status).json()["state"] == "ready"
        caption = next(iter(captions.sessions.values()))
        assert (caption.language, caption.translation, caption.translation_provider) == ("auto", translation, provider)
        assert client.post("/api/airplay/sessions", json={"channel_id": 1, "mode": mode}).status_code == 429
        assert f'LANGUAGE="{translation or "und"}"' in client.get(prefix + "/master.m3u8").text
        video = client.get(prefix + "/video.m3u8").text
        subtitles = client.get(prefix + "/subtitles.m3u8").text
        assert video.replace(".ts", ".vtt") == subtitles
        assert "#EXT-X-MEDIA-SEQUENCE:8" in video
        assert client.get(prefix + "/segment-00000008.ts").content == b"video"
        assert client.get(prefix + "/segment-00000000.ts").status_code == 404
        assert client.delete(status).status_code == 204
        assert not captions.sessions
        assert client.get(prefix + "/master.m3u8").status_code == 404


def test_vtt_matches_video_pts_and_marks_untranslated_text():
    manifest = """#EXTM3U
#EXTINF:2.000000,
#EXT-X-PROGRAM-DATE-TIME:2026-11-07T10:00:04.000+0000
segment-00000002.ts
"""
    number, duration, _, _ = next(source_segments(manifest))
    vtt = webvtt([
        {"start": 3.8, "end": 4.75, "text": "A < B & C"},
        {"start": 5.2, "end": 6.3, "text": "original", "translation_failed": True, "source_language": "fr"},
        {"start": 7, "end": 8, "text": "unrelated"},
    ], number * 2, duration, 361920)
    assert "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:361920" in vtt
    assert "00:00:00.000 --> 00:00:00.750\nA &lt; B &amp; C" in vtt
    assert "00:00:01.200 --> 00:00:02.000\n[FR] original" in vtt
    assert "unrelated" not in vtt
