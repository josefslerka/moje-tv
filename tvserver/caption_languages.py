"""Languages shared by the installed Whisper and Hy-MT2 checkpoints.

Hy-MT2's published list: https://github.com/Tencent-Hunyuan/Hy-MT2#supported-languages
Chinese writing variants share Whisper's zh token. Uyghur has no Whisper token.
"""

TRANSLATION_SOURCES = frozenset({
    "zh", "en", "fr", "pt", "es", "ja", "tr", "ru", "ar", "ko", "th", "it",
    "de", "vi", "ms", "id", "tl", "hi", "pl", "cs", "nl", "km", "my", "fa",
    "gu", "ur", "te", "mr", "he", "bn", "ta", "uk", "bo", "kk", "mn", "yue",
})


def choose_language(state: dict, probabilities: dict[str, float], at: float) -> str:
    candidate = max(probabilities, key=probabilities.get)
    confidence = float(probabilities[candidate])
    current = state.get("detected_language")
    repeated = state.get("candidate_language") == candidate
    if current is None or candidate == current or confidence >= .8 or (confidence >= .55 and repeated):
        state["detected_language"] = candidate
        state["language_confidence"] = confidence
        state.pop("candidate_language", None)
    elif confidence >= .55:
        state["candidate_language"] = candidate
    else:
        state.pop("candidate_language", None)
    # Recheck periodically, but don't pay for language detection on every 2s cue.
    state["language_check_at"] = at + (20 if confidence >= .75 and not state.get("candidate_language") else 6)
    return state["detected_language"]
