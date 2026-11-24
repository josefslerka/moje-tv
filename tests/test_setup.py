import importlib.util
import json
from pathlib import Path
import plistlib

import pytest

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_uses_supplied_paths_and_never_copies_api_key(tmp_path, monkeypatch):
    models = script("prepare_models")
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-test-value")
    whisper, translator = tmp_path / "speech", tmp_path / "translation"
    runtime = models.build_runtime(whisper, translator, python="python", paths=["site-packages"],
                                   ffmpeg="ffmpeg", openrouter=True)
    assert runtime["model_directory"] == str(whisper.resolve())
    assert runtime["translator"]["model_directory"] == str(translator.resolve())
    assert runtime["openrouter"] == {"model": "google/gemini-2.5-flash-lite"}
    assert "private-test-value" not in json.dumps(runtime)
    runtime = models.build_runtime(whisper, None, python="python", paths=[], ffmpeg="ffmpeg")
    assert "translator" not in runtime and "openrouter" not in runtime


def test_setup_rejects_wrong_translation_checkpoint(tmp_path):
    models = script("prepare_models")
    for name in ["model.safetensors", "tokenizer.json"]:
        (tmp_path / name).touch()
    (tmp_path / "config.json").write_text('{"quantization":{"bits":4}}')
    with pytest.raises(ValueError, match="8-bit"):
        models.validate_model(tmp_path, "translator")
    (tmp_path / "config.json").write_text('{"quantization":{"bits":8}}')
    models.validate_model(tmp_path, "translator")
    with pytest.raises(ValueError, match="weights.safetensors"):
        models.validate_model(tmp_path, "whisper")


def test_no_download_mode_and_existing_runtime_cannot_contact_network(tmp_path, monkeypatch):
    models = script("prepare_models")
    monkeypatch.setattr(models.sys, "platform", "darwin")
    monkeypatch.setattr(models.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(models, "find_spec", lambda name: object())
    monkeypatch.setattr(models.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(models, "download", lambda *args: pytest.fail("Network must not be contacted"))
    whisper = tmp_path / "speech"
    whisper.mkdir()
    (whisper / "config.json").write_text("{}")
    (whisper / "weights.safetensors").touch()
    runtime = tmp_path / "runtime.json"
    arguments = ["--runtime", str(runtime), "--whisper-path", str(whisper), "--asr-only", "--no-download"]
    assert models.main(arguments) == 0
    assert "translator" not in json.loads(runtime.read_text())
    with pytest.raises(ValueError, match="already exists"):
        models.main(arguments)


def test_launchd_configuration_is_specific_to_checkout_without_installing_it(tmp_path):
    launchd = script("render_launch_agent")
    root = tmp_path / "installation with spaces"
    output = tmp_path / "service.plist"
    assert launchd.main(["--root", str(root), "--output", str(output)]) == 0
    result = plistlib.loads(output.read_bytes())
    assert result["WorkingDirectory"] == str(root)
    assert result["ProgramArguments"] == [str(root / ".venv/bin/tvserver"), "--config", str(root / "config.toml"), "serve"]
    assert result["StandardOutPath"] == str(root / "data/server.log")
    assert "OPENROUTER_API_KEY" not in result["EnvironmentVariables"]
