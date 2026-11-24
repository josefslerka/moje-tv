"""Bounded, lazy local captions from player audio, with optional EN->CS translation."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import json
import logging
import math
from pathlib import Path
import secrets
import shutil
import time

from .caption_translation import TranslationRejected, translation_available
from .caption_cloud import MODEL as CLOUD_MODEL, OpenRouterTranslator, cloud_translation_available
from .caption_languages import TRANSLATION_SOURCES

log = logging.getLogger("tvserver.captions")
MAX_AUDIO_BYTES = 8 * 1024 * 1024


class CaptionError(RuntimeError):
    pass


class CaptionBusy(CaptionError):
    pass


class JobCancelled(Exception):
    pass


@dataclass
class Session:
    channel_id: int
    language: str = "en"
    translation: str | None = None
    translation_provider: str = "local"
    cloud_failures: int = 0
    cloud_retry_at: float = 0
    touched: float = field(default_factory=time.monotonic)
    inflight: str | None = None


@dataclass
class Job:
    session: str
    start: float
    end: float
    data: bytes
    language: str = "en"
    translation: str | None = None
    translation_provider: str = "local"
    media_format: str = "mp4"
    final: bool = False
    id: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    events: asyncio.Queue = field(default_factory=asyncio.Queue)


class CaptionService:
    def __init__(self, runtime: Path, *, usage=None):
        self.runtime = runtime
        self.usage = usage
        self.sessions: dict[str, Session] = {}
        self.jobs: asyncio.Queue = asyncio.Queue(maxsize=2)
        self.runner = None
        self.process = None
        self.stderr_task = None
        self.current: Job | None = None
        self.closed = False
        self.cloud_translator = None

    def status(self):
        data = {}
        try:
            data = json.loads(self.runtime.read_text())
            ffmpeg = data.get("ffmpeg") or shutil.which("ffmpeg")
            available = (Path(data["python"]).is_file()
                         and (Path(data["model_directory"]) / "config.json").is_file()
                         and (Path(data["model_directory"]) / "weights.safetensors").is_file()
                         and all(Path(path).is_dir() for path in data["pythonpath"])
                         and bool(ffmpeg and Path(ffmpeg).is_file()))
        except (OSError, ValueError, KeyError, TypeError):
            available = False
        translation = bool(available and translation_available(data))
        cloud_translation = bool(available and cloud_translation_available(data))
        return {"available": bool(available), "language": "en", "languages": ["en", "cs"],
                "translation": translation, "translation_languages": ["cs"] if translation else [],
                "cloud_translation": cloud_translation, "cloud_translation_model": CLOUD_MODEL,
                "auto_language": bool(available),
                "translation_source_languages": sorted(TRANSLATION_SOURCES) if translation or cloud_translation else [],
                "chunk_seconds": 2, "running": bool(self.process and self.process.returncode is None)}

    def _expire(self):
        for sid, session in list(self.sessions.items()):
            if not session.inflight and time.monotonic() - session.touched > 60:
                self.remove(sid)

    def create(self, channel_id: int, language: str = "en", translation: str | None = None,
               translation_provider: str = "local"):
        self._expire()
        if language not in {"en", "cs", "auto"}:
            raise ValueError("Nepodporovaný jazyk přepisu.")
        if translation not in {None, "cs"} or (translation and language not in {"en", "auto"}):
            raise ValueError("Překlad podporuje anglický nebo automaticky rozpoznaný přepis do češtiny.")
        if translation_provider not in {"local", "openrouter"} or (translation_provider == "openrouter" and not translation):
            raise ValueError("Placené API vyžaduje výslovnou volbu překladu do češtiny.")
        status = self.status()
        if not status["available"]:
            raise CaptionError("AI přepis není na serveru dostupný.")
        if translation and translation_provider == "local" and not status["translation"]:
            raise CaptionError("Český překlad není na serveru dostupný.")
        if translation_provider == "openrouter" and not status.get("cloud_translation"):
            raise CaptionError("Placený překlad Gemini není na serveru dostupný.")
        if len(self.sessions) >= 4:
            raise CaptionBusy("Přepis právě používá příliš mnoho přehrávačů.")
        sid = secrets.token_urlsafe(24)
        self.sessions[sid] = Session(channel_id, language, translation, translation_provider)
        return sid

    def remove(self, sid: str):
        self.sessions.pop(sid, None)
        if self.current and self.current.session == sid:
            self.current.cancelled.set()

    def submit(self, sid: str, start: float, end: float, data: bytes, *, media_format: str = "mp4", final: bool = False):
        self._expire()
        session = self.sessions.get(sid)
        if not session:
            raise KeyError(sid)
        if not (math.isfinite(start) and math.isfinite(end) and start >= 0 and 0 < end - start <= 30):
            raise ValueError("Neplatné časování zvuku.")
        if not data or len(data) > MAX_AUDIO_BYTES:
            raise ValueError("Neplatná velikost zvuku.")
        if media_format not in {"mp4", "mpegts"}:
            raise ValueError("Neplatný formát zvuku.")
        if session.inflight or self.jobs.full():
            raise CaptionBusy("Přepis ještě zpracovává předchozí zvuk.")
        job = Job(sid, start, end, data, language=session.language, translation=session.translation,
                  translation_provider=session.translation_provider, media_format=media_format, final=final)
        session.touched, session.inflight = time.monotonic(), job.id
        self.jobs.put_nowait(job)
        if self.runner is None or self.runner.done():
            self.runner = asyncio.create_task(self._run())
        return job

    async def events(self, job: Job):
        try:
            while True:
                event = await job.events.get()
                if event is None:
                    return
                yield event
        finally:
            job.cancelled.set()

    async def _stderr(self, stream):
        while line := await stream.readline():
            log.warning("ASR: %s", line.decode(errors="replace").strip()[:1000])

    async def _start_worker(self):
        if self.process and self.process.returncode is None:
            return
        await self._stop_worker()
        data = json.loads(self.runtime.read_text())
        ffmpeg = data.get("ffmpeg") or shutil.which("ffmpeg")
        if not ffmpeg:
            raise CaptionError("AI přepis není na serveru dostupný.")
        self.process = await asyncio.create_subprocess_exec(
            data["python"], "-u", str(Path(__file__).with_name("caption_worker.py")),
            "--runtime", str(self.runtime.resolve()), "--ffmpeg", ffmpeg,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=128 * 1024,
        )
        self.stderr_task = asyncio.create_task(self._stderr(self.process.stderr))
        line = await asyncio.wait_for(self.process.stdout.readline(), timeout=60)
        if not line or json.loads(line).get("type") != "ready":
            raise CaptionError("AI přepis se nepodařilo spustit.")

    async def _stop_worker(self):
        process, self.process = self.process, None
        if process and process.returncode is None:
            if self.current is None and process.stdin:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
        if process and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self.stderr_task:
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)
            self.stderr_task = None

    async def _line(self, job):
        read = asyncio.create_task(self.process.stdout.readline())
        cancel = asyncio.create_task(job.cancelled.wait())
        try:
            done, _ = await asyncio.wait((read, cancel), timeout=45, return_when=asyncio.FIRST_COMPLETED)
            if cancel in done:
                raise JobCancelled()
            if read not in done:
                raise TimeoutError("ASR timed out")
            line = read.result()
            if not line:
                raise CaptionError("AI přepis se přerušil.")
            return json.loads(line)
        finally:
            read.cancel()
            cancel.cancel()
            await asyncio.gather(read, cancel, return_exceptions=True)

    async def _cloud_cue(self, job, event):
        """The only paid path: original ASR cues from an explicitly paid session."""
        if job.cancelled.is_set() or job.session not in self.sessions:
            raise JobCancelled()
        session = self.sessions[job.session]
        text = event.get("text", "")
        source = event.get("detected_language") or event.get("language", "en")
        event = {**event, "source_text": text, "source_language": source,
                 "translation_requested": "cs", "translation_provider": "openrouter",
                 "translation_api_called": False}
        if not text.strip():
            return event
        if source == "cs":
            return {**event, "translation": "cs", "translation_skipped": True}
        if source not in TRANSLATION_SOURCES:
            return {**event, "translation_unsupported": True}
        if len(text) > 1600:
            return {**event, "translation_failed": True, "translation_error": "input_too_long",
                    "translation_retry_seconds": 0}
        if time.monotonic() < session.cloud_retry_at:
            return {**event, "translation_failed": True, "translation_error": "retry_wait",
                    "translation_retry_seconds": max(1, math.ceil(session.cloud_retry_at - time.monotonic()))}
        began = time.perf_counter()
        request = cancel = None
        try:
            if self.cloud_translator is None:
                self.cloud_translator = OpenRouterTranslator(json.loads(self.runtime.read_text()), recorder=self.usage)
            request = asyncio.create_task(self.cloud_translator.translate(text, channel_id=session.channel_id))
            cancel = asyncio.create_task(job.cancelled.wait())
            event["translation_api_called"] = True
            done, _ = await asyncio.wait((request, cancel), return_when=asyncio.FIRST_COMPLETED)
            if cancel in done or job.session not in self.sessions:
                raise JobCancelled()
            result = request.result()
            if not result.text:
                raise TranslationRejected("empty_or_unfinished_output")
            event.update(text=result.text, language="cs", translation="cs",
                         translation_cost_usd=result.cost_usd)
            session.cloud_failures = 0
            session.cloud_retry_at = 0
        except TranslationRejected as error:
            session.cloud_failures = 0
            session.cloud_retry_at = 0
            event.update(translation_failed=True, translation_error=error.reason, translation_retry_seconds=0)
        except JobCancelled:
            raise
        except Exception:
            session.cloud_failures = min(session.cloud_failures + 1, 5)
            delay = min(30, 2 ** session.cloud_failures)
            session.cloud_retry_at = time.monotonic() + delay
            event.update(translation_failed=True, translation_error="api_error", translation_retry_seconds=delay)
        finally:
            for task in (request, cancel):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (request, cancel) if task is not None), return_exceptions=True)
        event["translation_seconds"] = time.perf_counter() - began
        event["processing_seconds"] = event.get("processing_seconds", 0) + event["translation_seconds"]
        return event

    async def _run(self):
        try:
            while not self.closed:
                try:
                    job = await asyncio.wait_for(self.jobs.get(), timeout=60)
                except asyncio.TimeoutError:
                    self._expire()
                    await self._stop_worker()
                    continue
                self.current = job
                try:
                    if job.cancelled.is_set() or job.session not in self.sessions:
                        continue
                    await self._start_worker()
                    if job.cancelled.is_set():
                        raise JobCancelled()
                    message = {"id": job.id, "session": job.session, "start": job.start,
                               "end": job.end, "language": job.language,
                               "translation": job.translation if job.translation_provider == "local" else None,
                               "media_format": job.media_format,
                               "final": job.final,
                               "data": base64.b64encode(job.data).decode()}
                    self.process.stdin.write((json.dumps(message) + "\n").encode())
                    await self.process.stdin.drain()
                    while True:
                        event = await self._line(job)
                        if event.get("job") != job.id:
                            raise CaptionError("Neplatná odpověď přepisu.")
                        if event["type"] == "done":
                            break
                        if event["type"] == "cue" and job.translation_provider == "openrouter":
                            event = await self._cloud_cue(job, event)
                        job.events.put_nowait(event)
                        if event["type"] == "error":
                            break
                except JobCancelled:
                    await self._stop_worker()
                except Exception:
                    log.exception("AI přepis selhal")
                    job.events.put_nowait({"type": "error", "message": "AI přepis je dočasně nedostupný."})
                    await self._stop_worker()
                finally:
                    session = self.sessions.get(job.session)
                    if session and session.inflight == job.id:
                        session.inflight = None
                        session.touched = time.monotonic()
                    job.data = b""
                    job.events.put_nowait(None)
                    self.current = None
                    self.jobs.task_done()
        finally:
            await self._stop_worker()

    async def close(self):
        self.closed = True
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        await self._stop_worker()
        while not self.jobs.empty():
            job = self.jobs.get_nowait()
            job.cancelled.set()
            job.events.put_nowait(None)
            self.jobs.task_done()
        self.sessions.clear()
        if self.cloud_translator is not None:
            await self.cloud_translator.close()
            self.cloud_translator = None
