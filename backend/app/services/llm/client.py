"""Provider boundary: SDK details are confined to this file."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx
from gigachat import GigaChat
from gigachat.exceptions import GigaChatException, ResponseError


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
    base_url: str = "https://api.giga.chat/v1"
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
                base_url=os.getenv("GIGACHAT_BASE_URL", "https://api.giga.chat/v1"),
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

    async def complete(self, system: str, payload: dict, schema: dict) -> str:
        try:
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
                response = await client.achat({
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    "temperature": 0.1,
                    "max_tokens": self.settings.max_output_tokens,
                    "response_format": {"type": "json_schema", "schema": schema, "strict": True},
                })
                if not response.choices or response.choices[0].finish_reason != "stop":
                    raise LLMError("incomplete_model_response")
                content = response.choices[0].message.content
                if not content:
                    raise LLMError("empty_model_response")
                return content
        except ResponseError as exc:
            raise LLMError(f"gigachat_http_{exc.status_code}") from None
        except httpx.TimeoutException:
            raise LLMError("gigachat_timeout") from None
        except httpx.ConnectError as exc:
            code = ("gigachat_certificate_verification_failed"
                    if "CERTIFICATE_VERIFY_FAILED" in str(exc)
                    else "gigachat_connection_failed")
            raise LLMError(code) from None
        except (GigaChatException, httpx.HTTPError):
            raise LLMError("gigachat_request_failed") from None


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
