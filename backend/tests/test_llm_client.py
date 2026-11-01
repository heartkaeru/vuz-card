import asyncio
from types import SimpleNamespace

import pytest
import httpx
from gigachat.exceptions import ResponseError
from gigachat.models import Chat

from app.services.llm.client import ConfigurationError, GigaChatProvider, GigaChatSettings, LLMError


def settings(**kwargs):
    return GigaChatSettings(credentials="test-secret", model="test-model", scope="test-scope", **kwargs)


def test_missing_configuration_fails_without_api_call(monkeypatch):
    for name in ("GIGACHAT_CREDENTIALS", "GIGACHAT_MODEL", "GIGACHAT_SCOPE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigurationError):
        GigaChatSettings.from_env()


def test_non_https_endpoint_is_rejected():
    with pytest.raises(ConfigurationError):
        settings(base_url="http://untrusted.example")


def test_configuration_repr_does_not_show_credentials():
    assert "test-secret" not in repr(settings())


def test_sdk_payload_and_tls_configuration(monkeypatch):
    class FakeSDK:
        def __init__(self, **kwargs):
            assert kwargs["verify_ssl_certs"] is True
            assert kwargs["max_retries"] == 2

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def achat(self, payload):
            parsed = Chat.model_validate(payload)
            assert parsed.model == "test-model"
            assert payload["response_format"]["type"] == "json_schema"
            assert payload["response_format"]["strict"] is True
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content='{"facts": []}'))])

    monkeypatch.setattr("app.services.llm.client.GigaChat", FakeSDK)
    result = asyncio.run(GigaChatProvider(settings()).complete("system", {}, {"type": "object"}))
    assert result == '{"facts": []}'


def test_sdk_error_does_not_expose_headers_or_body(monkeypatch):
    class FailingSDK:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise ResponseError("https://api.giga.chat", 401, b"sensitive-body", None)

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("app.services.llm.client.GigaChat", FailingSDK)
    with pytest.raises(LLMError) as error:
        asyncio.run(GigaChatProvider(settings()).complete("system", {}, {}))
    assert str(error.value) == "gigachat_http_401"
    assert error.value.__suppress_context__


def test_truncated_response_is_not_accepted(monkeypatch):
    class TruncatedSDK:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def achat(self, payload):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")])

    monkeypatch.setattr("app.services.llm.client.GigaChat", TruncatedSDK)
    with pytest.raises(LLMError, match="incomplete_model_response"):
        asyncio.run(GigaChatProvider(settings()).complete("system", {}, {}))


@pytest.mark.parametrize("failure,code", [
    (httpx.ConnectError("CERTIFICATE_VERIFY_FAILED sensitive-details"),
     "gigachat_certificate_verification_failed"),
    (httpx.ConnectError("sensitive-network-details"), "gigachat_connection_failed"),
    (httpx.ReadTimeout("sensitive-timeout-details"), "gigachat_timeout"),
])
def test_transport_diagnostics_are_specific_and_redacted(monkeypatch, failure, code):
    class FailingSDK:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise failure

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("app.services.llm.client.GigaChat", FailingSDK)
    with pytest.raises(LLMError) as error:
        asyncio.run(GigaChatProvider(settings()).complete("system", {}, {}))
    assert str(error.value) == code
    assert error.value.__suppress_context__
