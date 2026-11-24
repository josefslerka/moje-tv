"""Server-side video recording and persistent, timed speech transcripts."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import html
import json
from pathlib import Path
import secrets
import shutil
import signal
import time

from . import db
from .airplay import source_segments
from .captions import CaptionBusy, CaptionService


RESERVE_BYTES = 512 * 1024 * 1024
ACTIVE_STATES = ("recording", "finishing")


class RecordingError(RuntimeError):
    pass


@dataclass
class Capture:
    id: str
    caption_id: str | None
    video: bool
    max_seconds: int
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    process: asyncio.subprocess.Process | None = None
    duration: float = 0
    progress_at: float = field(default_factory=time.monotonic)


def timestamp(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1_000 % 60:02d}.{ms % 1_000:03d}"


async def terminate(process):
    if process is None or process.returncode is not None:
        return
    try:
        process.send_signal(signal.SIGINT)
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        process.kill()
        await process.wait()
    except ProcessLookupError:
        pass


class RecordingService:
    def __init__(self, con, folder: Path, captions: CaptionService):
        self.con, self.folder, self.captions = con, folder, captions
        self.active: Capture | None = None

    def _update(self, rid: str, **values):
        assignments = ", ".join(f"{key}=?" for key in values)
        self.con.execute(f"UPDATE recordings SET {assignments} WHERE id=?", (*values.values(), rid))
        self.con.commit()

    def recover(self):
        """Keep the playable fragments after an interrupted server process."""
        for row in self.con.execute("SELECT * FROM recordings WHERE state IN ('recording','finishing')").fetchall():
            path = self.folder / row["id"]
            self._finish_video(path)
            self._update(row["id"], state="interrupted", finished_at=db.now(),
                         error="Nahrávání přerušil restart serveru. K dispozici je dosud uložená část.")
            self._clean_audio(path)

    def _ffmpeg(self) -> str:
        try:
            executable = json.loads(self.captions.runtime.read_text()).get("ffmpeg")
        except (OSError, ValueError):
            executable = None
        executable = executable or shutil.which("ffmpeg")
        if not executable or not Path(executable).is_file():
            raise RecordingError("Nahrávání potřebuje dostupný FFmpeg.")
        return executable

    def storage(self):
        parent = self.folder if self.folder.exists() else self.folder.parent
        return {"free_bytes": shutil.disk_usage(parent).free, "reserve_bytes": RESERVE_BYTES,
                "folder": str(self.folder.resolve())}

    def get(self, rid: str) -> dict | None:
        row = self.con.execute("SELECT * FROM recordings WHERE id=?", (rid,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        path = self.folder / rid
        video = path / "video.mp4"
        transcript = path / "transcript.jsonl"
        partial = path / "video.part"
        item.update(video=bool(row["video"]), has_video=video.is_file() and video.stat().st_size > 0,
                    has_text=transcript.is_file() and transcript.stat().st_size > 0,
                    size_bytes=sum(p.stat().st_size for p in (video, partial, transcript) if p.exists()))
        if self.active and self.active.id == rid:
            item["duration"] = self.active.duration
        return item

    def list(self):
        return [self.get(row[0]) for row in self.con.execute("SELECT id FROM recordings ORDER BY started_at DESC")]

    async def start(self, channel: dict, source: str, *, video: bool, text_mode: str | None, max_seconds: int):
        if self.active:
            raise RecordingError("Už probíhá jiné nahrávání. Nejdřív ho ukonči v Záznamech.")
        if not video and text_mode is None:
            raise ValueError("Vyber video, text, nebo obojí.")
        if text_mode not in {None, "asr", "translate-cs", "translate-cs-api"} or not 60 <= max_seconds <= 7200:
            raise ValueError("Neplatné nastavení záznamu.")
        ffmpeg = self._ffmpeg()
        if self.storage()["free_bytes"] < RESERVE_BYTES * 2:
            raise RecordingError("Pro nahrávání je potřeba alespoň 1 GB volného místa.")
        caption_id = None
        if text_mode:
            caption_id = self.captions.create(channel["id"], "auto", "cs" if text_mode != "asr" else None,
                                              "openrouter" if text_mode == "translate-cs-api" else "local")
        rid = secrets.token_hex(16)
        path = self.folder / rid
        try:
            path.mkdir(parents=True)
            self.con.execute(
                "INSERT INTO recordings (id,channel_id,channel_name,started_at,state,video,text_mode,max_seconds) "
                "VALUES (?,?,?,?, 'recording',?,?,?)",
                (rid, channel["id"], channel["name"], db.now(), video, text_mode, max_seconds))
            self.con.commit()
        except Exception:
            self.con.rollback()
            if caption_id:
                self.captions.remove(caption_id)
            if path.is_dir():
                path.rmdir()
            raise
        capture = self.active = Capture(rid, caption_id, video, max_seconds)
        capture.task = asyncio.create_task(self._run(capture, ffmpeg, source))
        return self.get(rid)

    def stop(self, rid: str):
        if self.active and self.active.id == rid:
            self._update(rid, state="finishing")
            self.active.stop.set()

    def delete(self, rid: str):
        item = self.get(rid)
        if not item:
            raise KeyError(rid)
        if item["state"] in ACTIVE_STATES:
            raise RecordingError("Probíhající záznam nejdřív zastav.")
        path = self.folder / rid
        if path.exists():
            shutil.rmtree(path)
        self.con.execute("DELETE FROM recordings WHERE id=?", (rid,))
        self.con.commit()

    @staticmethod
    def _finish_video(path: Path):
        partial = path / "video.part"
        if partial.exists() and partial.stat().st_size:
            partial.replace(path / "video.mp4")

    @staticmethod
    def _clean_audio(path: Path):
        for pattern in ("segment-*.ts*", "source.m3u8*"):
            for file in path.glob(pattern):
                file.unlink(missing_ok=True)

    async def _progress(self, capture: Capture):
        async for line in capture.process.stdout:
            if line.startswith(b"out_time_us="):
                try:
                    capture.duration = max(capture.duration, int(line.split(b"=", 1)[1]) / 1_000_000)
                except ValueError:
                    continue
                capture.progress_at = time.monotonic()
                self._update(capture.id, duration=capture.duration)

    async def _transcribe(self, capture: Capture, data: bytes, start: float, duration: float, final: bool, path: Path):
        async with asyncio.timeout(90):
            while True:
                try:
                    job = self.captions.submit(capture.caption_id, start, start + duration, data,
                                               media_format="mpegts", final=final)
                    break
                except CaptionBusy:
                    await asyncio.sleep(.25)
            with (path / "transcript.jsonl").open("a", encoding="utf-8") as transcript:
                async for cue in self.captions.events(job):
                    if cue["type"] == "error":
                        raise RecordingError("Textový přepis se přerušil.")
                    if cue["type"] != "cue" or not cue.get("text", "").strip():
                        continue
                    cue = {key: cue[key] for key in (
                        "start", "end", "text", "language", "source_text", "source_language",
                        "translation", "translation_failed", "translation_unsupported") if key in cue}
                    cue["end"] = min(cue["end"], start + duration)
                    if cue["end"] > cue["start"]:
                        transcript.write(json.dumps(cue, ensure_ascii=False) + "\n")
                        transcript.flush()

    async def _text(self, capture: Capture, path: Path):
        number, start = 0, 0.0
        playlist = path / "source.m3u8"
        try:
            while True:
                parts = list(source_segments(playlist.read_text())) if playlist.exists() else []
                pending = [part for part in parts if part[0] >= number]
                ended = capture.process.returncode is not None
                # Keep the newest audio fragment until the next arrives, so the
                # final fragment can flush the recognizer's partial window.
                if pending and (len(pending) > 1 or ended):
                    index, duration, _, filename = pending[0]
                    fragment = path / filename
                    await self._transcribe(capture, fragment.read_bytes(), start, duration,
                                           ended and len(pending) == 1, path)
                    fragment.unlink()
                    number, start = index + 1, start + duration
                elif ended:
                    return
                else:
                    await asyncio.sleep(.25)
        except (OSError, ValueError, KeyError, TimeoutError, RecordingError):
            self._update(capture.id, text_error="Textový přepis se přerušil. Uložený text je k dispozici.")
            if not capture.video:
                capture.stop.set()

    async def _run(self, capture: Capture, ffmpeg: str, source: str):
        path = self.folder / capture.id
        progress = text = None
        error, interrupted = "", False
        args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:1",
                "-readrate", "1", "-rw_timeout", "8000000", "-protocol_whitelist", "http,tcp,crypto",
                "-allowed_extensions", "ALL", "-allowed_segment_extensions", "ALL", "-extension_picky", "0",
                "-f", "hls", "-i", source]
        if capture.video:
            # Automatic stream selection keeps the highest-resolution video variant.
            args += ["-t", str(capture.max_seconds), "-sn", "-dn",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                     "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "video.part"]
        if capture.caption_id:
            args += ["-t", str(capture.max_seconds), "-map", "0:a:0", "-vn", "-c:a", "aac",
                     "-ar", "16000", "-ac", "1", "-b:a", "48k", "-f", "hls", "-hls_time", "4",
                     "-hls_list_size", "0", "-hls_flags", "temp_file+program_date_time",
                     "-hls_segment_filename", "segment-%08d.ts", "source.m3u8"]
        try:
            capture.process = await asyncio.create_subprocess_exec(
                *args, cwd=path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            progress = asyncio.create_task(self._progress(capture))
            if capture.caption_id:
                text = asyncio.create_task(self._text(capture, path))
            while capture.process.returncode is None and not capture.stop.is_set():
                if self.storage()["free_bytes"] < RESERVE_BYTES:
                    error = "Nahrávání se ukončilo kvůli nedostatku místa na disku."
                    break
                if time.monotonic() - capture.progress_at > 45:
                    error = "Zdroj přestal posílat obraz nebo zvuk."
                    break
                try:
                    await asyncio.wait_for(capture.stop.wait(), 1)
                except TimeoutError:
                    pass
            self._update(capture.id, state="finishing")
            requested_stop = capture.stop.is_set()
            await terminate(capture.process)
            await progress
            if capture.process.returncode and not requested_stop and not error:
                error = "Zdroj se nepodařilo celý nahrát. K dispozici je dosud uložená část."
            if text:
                await text
        except asyncio.CancelledError:
            interrupted = True
            error = "Nahrávání přerušilo ukončení serveru. K dispozici je dosud uložená část."
        except (OSError, ValueError):
            error = "Záznam se nepodařilo dokončit."
        finally:
            await terminate(capture.process)
            for task in (progress, text):
                if task:
                    task.cancel()
            await asyncio.gather(*(task for task in (progress, text) if task), return_exceptions=True)
            if capture.caption_id:
                self.captions.remove(capture.caption_id)
            self._finish_video(path)
            self._clean_audio(path)
            state = "interrupted" if interrupted else "error" if error else "ready"
            self._update(capture.id, state=state, error=error, finished_at=db.now(), duration=capture.duration)
            if self.active is capture:
                self.active = None

    def export(self, rid: str, extension: str) -> str:
        item = self.get(rid)
        if not item:
            raise KeyError(rid)
        file = self.folder / rid / "transcript.jsonl"
        cues = []
        if file.exists():
            for line in file.read_text().splitlines():
                try:
                    cues.append(json.loads(line))
                except ValueError:
                    break  # A file may end in a partially written cue after a crash.
        lines = ["WEBVTT", ""] if extension == "vtt" else []
        if extension == "txt":
            lines = [item["channel_name"], item["started_at"], ""]
        for number, cue in enumerate(cues, 1):
            text = " ".join(cue["text"].split())
            if cue.get("translation_failed") or cue.get("translation_unsupported"):
                text = f'[{cue.get("source_language", "en").upper()}] {text}'
            start, end = timestamp(cue["start"]), timestamp(cue["end"])
            if extension == "txt":
                lines.append(f"[{start}] {text}")
                if cue.get("source_text") and cue["source_text"] != cue["text"]:
                    lines.append(f'  [{cue.get("source_language", "").upper()}] {cue["source_text"]}')
            else:
                if extension == "srt":
                    lines.append(str(number))
                    start, end = start.replace(".", ","), end.replace(".", ",")
                lines.extend((f"{start} --> {end}", html.escape(text, quote=False), ""))
        return "\n".join(lines) + "\n"

    async def close(self):
        if self.active:
            capture = self.active
            capture.stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(capture.task), 4)
            except TimeoutError:
                capture.task.cancel()
                await asyncio.gather(capture.task, return_exceptions=True)
