"""A short rolling HLS stream with a server-generated WebVTT rendition."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import html
import json
import logging
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time

from .captions import CaptionBusy, CaptionService


log = logging.getLogger("tvserver.airplay")
SEGMENT_SECONDS = 2
PLAYLIST_SIZE = 6
IDLE_SECONDS = 35
SEGMENT_FILE = re.compile(r"segment-(\d{8,})\.(ts|vtt)\Z")
HLS_TYPE = "application/vnd.apple.mpegurl"


class AirPlayError(RuntimeError):
    pass


@dataclass
class Segment:
    number: int
    duration: float
    program_time: str
    video: bytes
    subtitles: str

    def filename(self, extension: str) -> str:
        return f"segment-{self.number:08d}.{extension}"


@dataclass
class AirPlaySession:
    id: str
    caption_id: str
    mode: str
    touched: float = field(default_factory=time.monotonic)
    segments: deque[Segment] = field(default_factory=lambda: deque(maxlen=PLAYLIST_SIZE + 4))
    task: asyncio.Task | None = None
    ended: bool = False
    error: str | None = None

    @property
    def state(self) -> str:
        if self.error:
            return "error"
        if len(self.segments) >= PLAYLIST_SIZE or (self.ended and self.segments):
            return "ready"
        return "preparing"

    def media(self, asset: str) -> tuple[bytes | str, str] | None:
        if self.state != "ready":
            return None
        if asset == "master.m3u8":
            language, name = ("und", "Přepis řeči") if self.mode == "asr" else ("cs", "Čeština")
            return (
                '#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-INDEPENDENT-SEGMENTS\n'
                f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="moje-tv",NAME="{name}",LANGUAGE="{language}",'
                'DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,URI="subtitles.m3u8"\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=3800000,AVERAGE-BANDWIDTH=2800000,RESOLUTION=1280x720,'
                'FRAME-RATE=25.000,CODECS="avc1.64001f,mp4a.40.2,wvtt",SUBTITLES="moje-tv"\n'
                'video.m3u8\n', HLS_TYPE,
            )
        if asset in {"video.m3u8", "subtitles.m3u8"}:
            segments = list(self.segments)[-PLAYLIST_SIZE:]
            extension = "ts" if asset == "video.m3u8" else "vtt"
            lines = ["#EXTM3U", "#EXT-X-VERSION:6", f"#EXT-X-TARGETDURATION:{SEGMENT_SECONDS}",
                     f"#EXT-X-MEDIA-SEQUENCE:{segments[0].number}"]
            for segment in segments:
                lines.extend((segment.program_time, f"#EXTINF:{segment.duration:.6f},", segment.filename(extension)))
            if self.ended:
                lines.append("#EXT-X-ENDLIST")
            return "\n".join(lines) + "\n", HLS_TYPE
        match = SEGMENT_FILE.fullmatch(asset)
        if match:
            extension = match[2]
            for segment in self.segments:
                if segment.filename(extension) == asset:
                    if extension == "ts":
                        return segment.video, "video/mp2t"
                    return segment.subtitles, "text/vtt"
        return None


def source_segments(playlist: str):
    """Read the atomic playlist written by our FFmpeg process."""
    duration, program_time = None, None
    for line in playlist.splitlines():
        if line.startswith("#EXTINF:"):
            duration = float(line[8:].split(",", 1)[0])
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            program_time = line
        else:
            match = SEGMENT_FILE.fullmatch(line)
            if match and match[2] == "ts" and duration is not None and program_time:
                yield int(match[1]), duration, program_time, line
                duration, program_time = None, None


def _timestamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1_000 % 60:02d}.{ms % 1_000:03d}"


def webvtt(cues: list[dict], start: float, duration: float, pts: int) -> str:
    lines = ["WEBVTT", f"X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:{pts % (1 << 33)}", ""]
    for cue in cues:
        text = " ".join(cue.get("text", "").split())
        begin = max(0, cue["start"] - start)
        end = min(duration, cue["end"] - start)
        if not text or end - begin < .001:
            continue
        if cue.get("translation_failed") or cue.get("translation_unsupported"):
            language = cue.get("source_language") or cue.get("language") or "en"
            text = f"[{language.upper()}] {text}"
        lines.extend((f"{_timestamp(begin)} --> {_timestamp(end)}", html.escape(text, quote=False), ""))
    return "\n".join(lines) + "\n"


async def _kill(process):
    if process is not None and process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def _video_pts(ffprobe: str, path: Path) -> int:
    process = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=start_pts",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 4)
        if process.returncode or not output.strip():
            raise AirPlayError("Chybí časování obrazu pro titulky.")
        return int(output.splitlines()[0])
    finally:
        await _kill(process)


def encoder_args(ffmpeg: str, source: str) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-readrate", "1", "-readrate_initial_burst", str(SEGMENT_SECONDS * PLAYLIST_SIZE),
        "-rw_timeout", "8000000", "-protocol_whitelist", "http,tcp,crypto",
        "-allowed_extensions", "ALL", "-allowed_segment_extensions", "ALL", "-extension_picky", "0",
        "-f", "hls", "-i", source, "-map", "0:v:0", "-map", "0:a:0",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,"
               "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-threads", "2",
        "-profile:v", "high", "-level:v", "3.1", "-pix_fmt", "yuv420p", "-r", "25",
        "-g", "50", "-keyint_min", "50", "-sc_threshold", "0", "-b:v", "2500k",
        "-maxrate", "2800k", "-bufsize", "2800k", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
        "-muxdelay", "0", "-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_list_size", "12",
        "-hls_delete_threshold", "6", "-hls_flags", "temp_file+independent_segments+delete_segments+program_date_time",
        "-hls_segment_filename", "segment-%08d.ts", "source.m3u8",
    ]


class AirPlayService:
    def __init__(self, captions: CaptionService):
        self.captions = captions
        self.session: AirPlaySession | None = None

    async def start(self, channel_id: int, source: str, mode: str) -> AirPlaySession:
        if self.session and self.get(self.session.id) and not self.session.error and not self.session.ended:
            raise CaptionBusy("AirPlay už běží v jiném přehrávači.")
        await self.stop()
        if self.session is not None:
            raise CaptionBusy("AirPlay už běží v jiném přehrávači.")
        runtime = json.loads(self.captions.runtime.read_text())
        ffmpeg = runtime.get("ffmpeg") or shutil.which("ffmpeg")
        ffprobe = str(Path(ffmpeg).with_name("ffprobe")) if ffmpeg else None
        if not ffmpeg or not (Path(ffmpeg).is_file() and Path(ffprobe).is_file()):
            raise AirPlayError("Příprava videa pro AirPlay není dostupná.")
        caption_id = self.captions.create(channel_id, "auto", "cs" if mode != "asr" else None,
                                          "openrouter" if mode == "translate-cs-api" else "local")
        session = AirPlaySession(secrets.token_urlsafe(24), caption_id, mode)
        self.session = session
        session.task = asyncio.create_task(self._run(session, ffmpeg, ffprobe, source))
        return session

    def get(self, session_id: str) -> AirPlaySession | None:
        session = self.session
        if session and session.id == session_id and time.monotonic() - session.touched < IDLE_SECONDS:
            return session
        return None

    async def stop(self, session_id: str | None = None):
        session = self.session
        if session is None or (session_id is not None and session.id != session_id):
            return
        self.session = None
        self.captions.remove(session.caption_id)
        if session.task:
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)

    async def _captions(self, session: AirPlaySession, start: float, video: bytes) -> list[dict]:
        async with asyncio.timeout(20):
            while True:
                try:
                    job = self.captions.submit(session.caption_id, start, start + SEGMENT_SECONDS, video,
                                               media_format="mpegts")
                    break
                except CaptionBusy:
                    await asyncio.sleep(.2)
            cues = []
            async for event in self.captions.events(job):
                if event["type"] == "error":
                    raise AirPlayError("Zvuk pro titulky se nepodařilo přepsat.")
                if event["type"] == "cue":
                    cues.append(event)
            return cues

    async def _run(self, session: AirPlaySession, ffmpeg: str, ffprobe: str, source: str):
        with tempfile.TemporaryDirectory(prefix="moje-tv-airplay-") as folder:
            path = Path(folder)
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *encoder_args(ffmpeg, source), cwd=path,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await self._publish(session, process, ffprobe, path)
            except AirPlayError as error:
                session.error = str(error)
            except (OSError, ValueError, KeyError, TimeoutError) as error:
                log.warning("AirPlay se přerušil (%s)", type(error).__name__)
                session.error = "Přenos pro AirPlay se nepodařilo připravit."
            finally:
                self.captions.remove(session.caption_id)
                await _kill(process)
                if self.session is session and not self.get(session.id):
                    self.session = None

    async def _publish(self, session: AirPlaySession, process, ffprobe: str, path: Path):
        playlist_path = path / "source.m3u8"
        number = 0
        last_progress = time.monotonic()
        while self.get(session.id):
            playlist = playlist_path.read_text() if playlist_path.exists() else ""
            available = list(source_segments(playlist))
            if available and available[0][0] > number:
                raise AirPlayError("Příprava titulků nestíhá živé vysílání.")
            pending = next((part for part in available if part[0] == number), None)
            if pending:
                _, duration, program_time, filename = pending
                if not 0 < duration <= SEGMENT_SECONDS + .04:
                    raise AirPlayError("Video má pro AirPlay nepodporované časování.")
                segment_path = path / filename
                video = segment_path.read_bytes()
                pts = await _video_pts(ffprobe, segment_path)
                start = number * SEGMENT_SECONDS
                cues = await self._captions(session, start, video)
                session.segments.append(Segment(number, duration, program_time, video, webvtt(cues, start, duration, pts)))
                number += 1
                last_progress = time.monotonic()
            elif process.returncode is not None:
                if process.returncode != 0 or not session.segments:
                    raise AirPlayError("Zdroj videa pro AirPlay neodpovídá.")
                session.ended = True
                return
            elif time.monotonic() - last_progress > 20:
                raise AirPlayError("Zdroj videa pro AirPlay přestal posílat obraz.")
            else:
                await asyncio.sleep(.2)
