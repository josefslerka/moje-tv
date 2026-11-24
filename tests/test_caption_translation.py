import json

import pytest

from tvserver.caption_translation import LocalTranslator, TranslationRejected, translation_available
from tvserver.caption_worker import caption_payload
from tvserver.caption_languages import TRANSLATION_SOURCES, choose_language


def test_translation_preserves_source_text_and_marks_output_language():
    class Translator:
        def translate(self, text):
            assert text == "The proposal has not been approved."
            return "Návrh nebyl schválen."

    cue = caption_payload("The proposal has not been approved.", "en", "cs", {}, Translator)
    assert cue["text"] == "Návrh nebyl schválen."
    assert cue["source_text"] == "The proposal has not been approved."
    assert cue["language"] == cue["translation"] == "cs"
    assert cue["source_language"] == "en"


@pytest.mark.parametrize("language,text", [("en", "English"), ("cs", "Čeština"), ("en", "")])
def test_plain_asr_never_loads_translator(language, text):
    def fail():
        pytest.fail("Plain ASR must not load a translation model")
    assert caption_payload(text, language, None, {}, fail) == {
        "text": text, "language": language, "translation": False,
    }


def test_silence_does_not_load_translation_model():
    def fail():
        pytest.fail("Silence must not load a translation model")
    cue = caption_payload("", "en", "cs", {}, fail)
    assert cue["text"] == "" and not cue.get("translation_failed")


@pytest.mark.parametrize("language", ["fr", "de", "es", "uk", "ja"])
def test_supported_detected_language_is_translated_directly(language):
    class Translator:
        def translate(self, text):
            assert text == "Original-language input"
            return "Český výstup"
    cue = caption_payload("Original-language input", language, "cs", {}, Translator)
    assert cue["source_language"] == language
    assert cue["language"] == cue["translation"] == "cs"


def test_czech_and_unsupported_languages_do_not_load_translator():
    def fail():
        pytest.fail("Translator must not load for Czech or an unsupported source language")
    czech = caption_payload("Česká řeč", "cs", "cs", {}, fail)
    assert czech["text"] == "Česká řeč" and czech["translation_skipped"]
    unsupported = caption_payload("Svenska", "sv", "cs", {}, fail)
    assert unsupported["text"] == "Svenska" and unsupported["language"] == "sv"
    assert unsupported["translation_unsupported"] and not unsupported.get("translation_failed")


def test_language_detection_resists_uncertain_flips_and_rechecks_afterward():
    state = {}
    assert choose_language(state, {"en": .95, "fr": .05}, 0) == "en"
    assert state["language_check_at"] == 20
    assert choose_language(state, {"en": .4, "fr": .6}, 20) == "en"
    assert state["language_check_at"] == 26
    assert choose_language(state, {"en": .4, "fr": .6}, 26) == "fr"
    assert choose_language(state, {"en": .94, "fr": .06}, 32) == "en"
    assert {"en", "fr", "de", "ja", "cs"} <= TRANSLATION_SOURCES
    assert "sv" not in TRANSLATION_SOURCES
def test_rejected_caption_keeps_english_and_next_caption_recovers_in_same_session():
    calls = []

    class Flaky:
        def translate(self, text):
            calls.append(text)
            if len(calls) == 1:
                raise TranslationRejected("output_limit")
            return "Obnovený překlad"

    state = {}
    failed = caption_payload("First caption", "en", "cs", state, Flaky)
    assert failed["translation_failed"] and failed["translation"] is False
    assert failed["text"] == failed["source_text"] == "First caption"
    assert failed["translation_error"] == "output_limit"
    recovered = caption_payload("Second caption", "en", "cs", state, Flaky)
    assert recovered["text"] == "Obnovený překlad" and recovered["translation"] == "cs"
    assert not recovered.get("translation_failed")
    assert calls == ["First caption", "Second caption"]


def test_model_failure_waits_briefly_and_recovers_without_new_session(monkeypatch):
    clock, calls = [100.0], []
    monkeypatch.setattr("tvserver.caption_worker.time.monotonic", lambda: clock[0])

    class Flaky:
        def translate(self, text):
            calls.append(text)
            if len(calls) == 1:
                raise RuntimeError("Temporarily unavailable")
            return "Obnoveno"

    state = {}
    failed = caption_payload("First", "en", "cs", state, Flaky)
    assert failed["translation_retry_seconds"] == 2
    waiting = caption_payload("During cooldown", "en", "cs", state, Flaky)
    assert waiting["translation_failed"] and calls == ["First"]
    clock[0] += 2
    recovered = caption_payload("After cooldown", "en", "cs", state, Flaky)
    assert recovered["translation"] == "cs" and recovered["text"] == "Obnoveno"
    assert not state.get("translation_failures") and not state.get("translation_retry_at")


def test_repeated_model_failures_have_bounded_backoff_and_can_still_recover(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("tvserver.caption_worker.time.monotonic", lambda: clock[0])

    class Broken:
        def translate(self, text):
            raise RuntimeError("Unavailable")

    class Working:
        def translate(self, text):
            return "Funguje"

    state, delays = {}, []
    for _ in range(9):
        failed = caption_payload("Text", "en", "cs", state, Broken)
        delays.append(failed["translation_retry_seconds"])
        clock[0] = state["translation_retry_at"]
    assert delays[:3] == [2, 4, 8] and max(delays) == 30
    assert caption_payload("Recovered", "en", "cs", state, Working)["text"] == "Funguje"


def test_oversized_input_is_rejected_before_loading_model():
    translator = LocalTranslator({"translator": {}})
    with pytest.raises(TranslationRejected) as error:
        translator.translate("x" * 1601)
    assert error.value.reason == "input_too_long"
    assert translator.model is None


def test_status_checks_optional_local_model_without_importing_mlx(tmp_path):
    model, dependencies = tmp_path / "model", tmp_path / "runtime"
    model.mkdir()
    (dependencies / "mlx_lm").mkdir(parents=True)
    (dependencies / "mlx_lm/__init__.py").touch()
    for name in ["model.safetensors", "tokenizer.json"]:
        (model / name).touch()
    config = {"translator": {"model_directory": str(model), "pythonpath": [str(dependencies)]}}
    assert not translation_available(config)
    (model / "config.json").write_text(json.dumps({"quantization": {"bits": 8}}))
    assert translation_available(config)
    (model / "config.json").write_text(json.dumps({"quantization": {"bits": 4}}))
    assert not translation_available(config)
    assert not translation_available({})
