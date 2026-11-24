"""Isolated MLX caption worker; optional local translation, JSON lines protocol.

Audio comes from the player's already decrypted MP4 audio fragments. FFmpeg is
restricted to local pipe input; the worker never opens a stream URL.
"""
from __future__ import annotations

import argparse
import base64
import importlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

if __package__:
    from .caption_translation import LocalTranslator, TranslationRejected
    from .caption_languages import TRANSLATION_SOURCES, choose_language
else:
    from caption_translation import LocalTranslator, TranslationRejected
    from caption_languages import TRANSLATION_SOURCES, choose_language


def emit(**event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def caption_payload(text, language, translation, state, get_translator):
    payload = {"text": text, "language": language, "translation": False}
    if not translation:
        return payload
    payload.update(source_text=text, source_language=language, translation_requested=translation)
    if not text:
        return payload
    if language == translation:
        payload.update(translation=translation, translation_skipped=True)
        return payload
    if language not in TRANSLATION_SOURCES:
        payload["translation_unsupported"] = True
        return payload
    began = time.perf_counter()
    retry_at = state.get("translation_retry_at", 0)
    if time.monotonic() < retry_at:
        payload.update(translation_failed=True, translation_error="retry_wait",
                       translation_retry_seconds=max(1, math.ceil(retry_at - time.monotonic())))
    else:
        try:
            translated = get_translator().translate(text)
            if not translated:
                raise TranslationRejected("empty_or_unfinished_output")
            payload.update(text=translated, language=translation, translation=translation)
            state.pop("translation_failures", None)
            state.pop("translation_retry_at", None)
        except TranslationRejected as error:
            # A single pathological fragment must not turn off the whole session.
            state.pop("translation_failures", None)
            state.pop("translation_retry_at", None)
            payload.update(translation_failed=True, translation_error=error.reason,
                           translation_retry_seconds=0)
            print(f"Caption translation: rejected={error.reason} input_chars={len(text)} retry=next_caption",
                  file=sys.stderr, flush=True)
        except Exception as error:
            failures = min(state.get("translation_failures", 0) + 1, 5)
            retry_seconds = min(30, 2 ** failures)
            state.update(translation_failures=failures,
                         translation_retry_at=time.monotonic() + retry_seconds)
            payload.update(translation_failed=True, translation_error="model_error",
                           translation_retry_seconds=retry_seconds)
            print(f"Caption translation: failure={type(error).__name__} retry_seconds={retry_seconds}",
                  file=sys.stderr, flush=True)
    payload["translation_seconds"] = time.perf_counter() - began
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--ffmpeg", required=True)
    args = parser.parse_args()
    runtime = json.loads(args.runtime.read_text())
    for directory in reversed(runtime["pythonpath"]):
        sys.path.insert(0, directory)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    import numpy as np
    import mlx.core as mx
    import mlx_whisper
    from mlx_whisper.audio import log_mel_spectrogram, pad_or_trim, N_FRAMES
    holder = importlib.import_module("mlx_whisper.transcribe").ModelHolder
    model = str(Path(runtime["model_directory"]).resolve())
    mx.set_default_device(mx.gpu)
    recognizer = holder.get_model(model, mx.float16)
    mx.eval(recognizer.parameters())
    states = {}
    translator = None

    def get_translator():
        nonlocal translator
        if translator is None:
            translator = LocalTranslator(runtime)
        return translator

    emit(type="ready", languages=["en", "cs", "auto"])
    for line in sys.stdin:
        job = None
        try:
            job = json.loads(line)
            language = job.get("language", "en")
            translation = job.get("translation")
            if language not in {"en", "cs", "auto"}:
                raise ValueError("Unsupported transcription language")
            if translation not in {None, "cs"} or (translation and language not in {"en", "auto"}):
                raise ValueError("Unsupported translation")
            media_format = job.get("media_format", "mp4")
            if media_format not in {"mp4", "mpegts"}:
                raise ValueError("Unsupported media format")
            raw = base64.b64decode(job["data"], validate=True)
            decoded = subprocess.run([
                args.ffmpeg, "-hide_banner", "-loglevel", "error",
                "-protocol_whitelist", "pipe", "-f", media_format, "-i", "pipe:0",
                "-map", "0:a:0", "-vn", "-t", "30", "-ac", "1", "-ar", "16000",
                "-f", "s16le", "pipe:1",
            ], input=raw, capture_output=True, timeout=15)
            if decoded.returncode or not decoded.stdout:
                raise RuntimeError("Audio fragment could not be decoded")
            audio = np.frombuffer(decoded.stdout, dtype="<i2").astype(np.float32) / 32768
            # AAC padding can exceed the advertised media interval slightly.
            samples = round((job["end"] - job["start"]) * 16000)
            audio = audio[:samples]
            if media_format == "mpegts" and len(audio) < samples:
                # Match the fixed AirPlay video segment boundary despite AAC packet padding.
                audio = np.pad(audio, (0, samples - len(audio)))
            sid = job["session"]
            state = states.get(sid)
            if (state is None or state["language"] != language or state["translation"] != translation
                    or abs(state["end"] - job["start"]) > .35):
                state = {"pending": np.empty(0, np.float32), "start": job["start"],
                         "tail": np.empty(0, np.float32), "previous": "", "language": language,
                         "translation": translation}
            state["end"] = job["end"]
            state["used"] = time.monotonic()
            state["pending"] = np.concatenate((state["pending"], audio))
            states[sid] = state
            detection_seconds = 0.0

            def detect(samples, at):
                nonlocal detection_seconds
                began = time.perf_counter()
                mel = log_mel_spectrogram(samples, n_mels=recognizer.dims.n_mels)
                mel = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
                _, probabilities = recognizer.detect_language(mel)
                mx.synchronize()
                previous = state.get("detected_language")
                chosen = choose_language(state, probabilities, at)
                detection_seconds += time.perf_counter() - began
                if previous != chosen:
                    state["previous"] = ""
                    state["tail"] = np.empty(0, np.float32)
                    print(f"Caption language: {chosen} confidence={state['language_confidence']:.2f}",
                          file=sys.stderr, flush=True)
                return chosen

            if language == "auto":
                history = np.concatenate((state.get("language_audio", np.empty(0, np.float32)), audio))[-128000:]
                state["language_audio"] = history
                if (job["start"] >= state.get("language_check_at", -float("inf"))
                        and len(history) and float(np.sqrt(np.mean(history * history))) > .002):
                    # Use the beginning of a long fragment for its first cues;
                    # short fragments benefit from already heard context.
                    samples = state["pending"][:128000] if len(state["pending"]) >= 64000 else history
                    detect(samples, job["start"])
            for old in list(states):
                if old != sid and time.monotonic() - states[old]["used"] > 60:
                    del states[old]
            if job.get("final") and len(state["pending"]) % 32000:
                # A recording can stop midway through the last two-second window.
                padding = 32000 - len(state["pending"]) % 32000
                state["pending"] = np.pad(state["pending"], (0, padding))
            while len(state["pending"]) >= 32000:
                current, state["pending"] = state["pending"][:32000], state["pending"][32000:]
                start = state["start"]
                state["start"] += 2
                window = np.concatenate((state["tail"], current))
                state["tail"] = current[-16000:]
                text = ""
                detection_before_window = detection_seconds
                began = time.perf_counter()
                if float(np.sqrt(np.mean(current * current))) > .002:
                    actual_language = state.get("detected_language") if language == "auto" else language
                    if actual_language is None:
                        actual_language = detect(window, start)
                    result = mlx_whisper.transcribe(
                        window, path_or_hf_repo=model, language=actual_language, task="transcribe",
                        temperature=0, fp16=True, condition_on_previous_text=False,
                        verbose=None, word_timestamps=False,
                    )
                    mx.synchronize()
                    text = result["text"].strip()
                    words, previous = text.split(), state["previous"].split()
                    normal = lambda word: re.sub(r"\W", "", word).casefold()
                    for count in range(min(12, len(words), len(previous)), 0, -1):
                        if [normal(w) for w in words[:count]] == [normal(w) for w in previous[-count:]]:
                            text = " ".join(words[count:])
                            break
                    state["previous"] = " ".join((previous + text.split())[-16:])
                else:
                    state["previous"] = ""
                asr_seconds = time.perf_counter() - began - (detection_seconds - detection_before_window)
                actual_language = state.get("detected_language", "en") if language == "auto" else language
                payload = caption_payload(text, actual_language, translation, state, get_translator)
                emit(type="cue", job=job["id"], start=start, end=start + 2, **payload,
                     asr_seconds=asr_seconds, language_detection_seconds=detection_seconds,
                     detected_language=actual_language,
                     language_confidence=state.get("language_confidence") if language == "auto" else None,
                     processing_seconds=time.perf_counter() - began + detection_before_window)
                detection_seconds = 0.0
            emit(type="done", job=job["id"])
        except Exception as error:
            print(f"Caption worker: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            emit(type="error", job=job.get("id") if isinstance(job, dict) else None,
                 message="Zvuk se nepodařilo přepsat.")


if __name__ == "__main__":
    main()
