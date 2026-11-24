import asyncio
import json

import httpx
import pytest

from tvserver.caption_cloud import (ENDPOINT, MODEL, OpenRouterTranslator,
                                    cloud_translation_available)
from tvserver.caption_translation import TranslationRejected


def runtime(tmp_path):
    env = tmp_path / "test.env"
    env.write_text('export OPENROUTER_API_KEY="sk-or-v1-' + 'x' * 40 + '"\n')
    return {"openrouter": {"model": MODEL, "env_file": str(env)}}


def test_paid_capability_needs_explicit_configuration_and_valid_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert not cloud_translation_available({})
    assert not cloud_translation_available({"openrouter": {}})
    config = runtime(tmp_path)
    assert cloud_translation_available(config)
    config["openrouter"]["model"] = "other-model"
    assert not cloud_translation_available(config)
    assert not cloud_translation_available({"openrouter": []})


def test_only_saved_text_is_sent_to_fixed_paid_model_with_price_ceiling(tmp_path):
    requests = []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert str(request.url) == ENDPOINT
        assert body["model"] == MODEL
        assert body["max_tokens"] == 128
        assert body["reasoning"] == {"enabled": False}
        assert body["provider"] == {"sort": "latency", "max_price": {"prompt": .10, "completion": .40}}
        assert body["messages"][0]["content"].endswith("The proposal has not been approved.")
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": "Návrh nebyl schválen."}}], "usage": {"cost": .000012}})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        translator = OpenRouterTranslator(runtime(tmp_path), client=client)
        try:
            result = await translator.translate("The proposal has not been approved.")
            assert result.text == "Návrh nebyl schválen." and result.cost_usd == .000012
            assert not hasattr(result, "key")
        finally:
            await translator.close()
    asyncio.run(run())
    assert len(requests) == 1


@pytest.mark.parametrize("content,finish", [(None, "stop"), ("", "stop"), ("partial", "length")])
def test_empty_or_truncated_paid_output_is_rejected_without_retry(tmp_path, content, finish):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": finish,
                                                    "message": {"content": content}}]})

    async def run():
        translator = OpenRouterTranslator(runtime(tmp_path), client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        try:
            with pytest.raises(TranslationRejected):
                await translator.translate("A caption")
        finally:
            await translator.close()
    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 402, 429, 503])
def test_api_errors_do_not_retry_or_expose_provider_error_body(tmp_path, status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "PRIVATE_PROVIDER_BODY"}})

    async def run():
        translator = OpenRouterTranslator(runtime(tmp_path), client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        try:
            with pytest.raises(RuntimeError, match="^OpenRouter request failed$"):
                await translator.translate("A caption")
        finally:
            await translator.close()
    asyncio.run(run())
    assert len(calls) == 1


def test_silence_and_oversized_text_never_open_paid_connection(tmp_path):
    async def run():
        translator = OpenRouterTranslator(runtime(tmp_path))
        assert (await translator.translate("  ")).text == ""
        with pytest.raises(TranslationRejected, match="input_too_long"):
            await translator.translate("x" * 1601)
        assert translator.client is None
    asyncio.run(run())
