"""Provider boundary: SDK details are confined to this file."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx
from gigachat import GigaChat
from gigachat.exceptions import GigaChatException, ResponseError
from logging_config import describe_error

LOGGER = logging.getLogger("gigachat")


class LLMError(RuntimeError):
    """Safe error code; never exposes SDK response bodies or authorization headers."""


class ConfigurationError(ValueError):
    pass


class JSONProvider(Protocol):
    name: str
    model: str

    async def complete(self, system: str, payload: dict, schema: dict) -> str: ...


@dataclass(frozen=True)
class GigaChatSettings:
    credentials: str = field(repr=False)
    model: str
    scope: str
    base_url: str = "https://gigachat.devices.sberbank.ru/api/v1"
    timeout: float = 60
    max_retries: int = 2
    max_output_tokens: int = 4096
    ca_bundle_file: str | None = None

    def __post_init__(self):
        if not all(value.strip() for value in (self.credentials, self.model, self.scope)):
            raise ConfigurationError("Set GIGACHAT_CREDENTIALS, GIGACHAT_MODEL and GIGACHAT_SCOPE")
        if not self.base_url.startswith("https://"):
            raise ConfigurationError("GIGACHAT_BASE_URL must use HTTPS")
        if self.timeout <= 0 or not 0 <= self.max_retries <= 3 or self.max_output_tokens < 1:
            raise ConfigurationError("Invalid timeout, retry count or output token limit")

    @classmethod
    def from_env(cls):
        try:
            return cls(
                credentials=os.getenv("GIGACHAT_CREDENTIALS", ""),
                model=os.getenv("GIGACHAT_MODEL", ""),
                scope=os.getenv("GIGACHAT_SCOPE", ""),
                base_url=os.getenv("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru/api/v1"),
                timeout=float(os.getenv("GIGACHAT_TIMEOUT", "60")),
                max_retries=int(os.getenv("GIGACHAT_MAX_RETRIES", "2")),
                max_output_tokens=int(os.getenv("GIGACHAT_MAX_OUTPUT_TOKENS", "4096")),
                ca_bundle_file=os.getenv("GIGACHAT_CA_BUNDLE_FILE") or None,
            )
        except ValueError as exc:
            if isinstance(exc, ConfigurationError):
                raise
            raise ConfigurationError("Invalid numeric GigaChat configuration") from None


class GigaChatProvider:
    name = "gigachat"

    def __init__(self, settings: GigaChatSettings):
        self.settings = settings
        self.model = settings.model
        # Personal credentials allow one in-flight request. Wait between requests,
        # so a chat or match can run while a collection session is still open.
        self._request_lock = asyncio.Lock() if settings.scope == "GIGACHAT_API_PERS" else nullcontext()
        self._session: ContextVar[GigaChat | None] = ContextVar("gigachat_session", default=None)

    @asynccontextmanager
    async def session(self):
        """Reuse the SDK's transport and authorization within a single collection job."""
        current = self._session.get()
        if current is not None:
            yield current
            return
        async with GigaChat(
            credentials=self.settings.credentials,
            model=self.model,
            scope=self.settings.scope,
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
            max_retries=self.settings.max_retries,
            verify_ssl_certs=True,
            ca_bundle_file=self.settings.ca_bundle_file,
        ) as client:
            token = self._session.set(client)
            try:
                yield client
            finally:
                self._session.reset(token)

    async def complete(self, system: str, payload: dict, schema: dict) -> str:
        choice = await self.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], response_format={"type": "json_schema", "schema": schema, "strict": True})
        if choice.finish_reason != "stop":
            LOGGER.error(
                "GigaChat вернул неполный ответ: причина завершения=%s",
                choice.finish_reason or "не указана",
            )
            raise LLMError("incomplete_model_response")
        if not choice.message.content:
            LOGGER.error("Запрос к GigaChat завершился ошибкой: %s", describe_error("empty_model_response"))
            raise LLMError("empty_model_response")
        return choice.message.content

    async def chat(self, messages: list[dict], **options):
        try:
            async with self._request_lock, self.session() as client:
                response = await client.achat({
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": self.settings.max_output_tokens,
                    **options,
                })
                if not response.choices:
                    LOGGER.error("Запрос к GigaChat завершился ошибкой: %s",
                                 describe_error("incomplete_model_response"))
                    raise LLMError("incomplete_model_response")
                return response.choices[0]
        except ResponseError as exc:
            error = LLMError(f"gigachat_http_{exc.status_code}")
            LOGGER.error("Запрос к GigaChat завершился ошибкой: %s", describe_error(error))
            raise error from None
        except httpx.TimeoutException:
            error = LLMError("gigachat_timeout")
            LOGGER.error("Запрос к GigaChat завершился ошибкой: %s", describe_error(error))
            raise error from None
        except httpx.ConnectError as exc:
            code = ("gigachat_certificate_verification_failed"
                    if "CERTIFICATE_VERIFY_FAILED" in str(exc)
                    else "gigachat_connection_failed")
            error = LLMError(code)
            LOGGER.error("Запрос к GigaChat завершился ошибкой: %s", describe_error(error))
            raise error from None
        except (GigaChatException, httpx.HTTPError):
            error = LLMError("gigachat_request_failed")
            LOGGER.error("Запрос к GigaChat завершился ошибкой: %s", describe_error(error))
            raise error from None


class FixtureProvider:
    """Explicit offline replay. This is NOT an extraction algorithm or a real API call."""

    name = "fixture"
    model = "offline-replay"

    def __init__(self, responses: list[dict]):
        self.responses = iter(responses)

    @classmethod
    def from_file(cls, path: Path):
        return cls(json.loads(path.read_text(encoding="utf-8-sig")))

    async def complete(self, system: str, payload: dict, schema: dict) -> str:
        try:
            return json.dumps(next(self.responses), ensure_ascii=False)
        except StopIteration:
            raise LLMError("fixture_exhausted") from None
