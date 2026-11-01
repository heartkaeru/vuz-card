"""Interpret a first user request without resolving identities or changing CRM data."""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, ValidationError, model_validator

from .client import JSONProvider, LLMError
from .models import RunInfo, StrictModel


class UserRequest(StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    message: str = Field(min_length=1, max_length=8000)


class RequestProposal(StrictModel):
    intent: Literal["prepare_materials", "find_institution", "unsupported"]
    outputs: list[Literal["brief", "presentation"]] = Field(max_length=2)
    institution_name: str | None = Field(description="Дословный фрагмент сообщения в исходном падеже, без нормализации названия")
    city: str | None = Field(description="Дословный фрагмент сообщения в исходном падеже")
    website_candidate: str | None
    meeting_goal: str | None
    meeting_date_text: str | None
    template: str | None

    @model_validator(mode="after")
    def consistent_intent(self):
        if len(self.outputs) != len(set(self.outputs)):
            raise ValueError("Duplicate outputs")
        if self.intent != "prepare_materials" and self.outputs:
            raise ValueError("Outputs require prepare_materials intent")
        return self


class InterpretedRequest(StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    run: RunInfo
    request: RequestProposal
    next_step: Literal["clarify", "resolve_institution", "unsupported"]
    clarification_codes: list[Literal["institution_required", "output_required"]]
    questions: list[str]


class TextSpan(StrictModel):
    first_token: int = Field(ge=0, description="ID первого токена, включительно")
    last_token: int = Field(ge=0, description="ID последнего токена, включительно")

    @model_validator(mode="after")
    def ordered(self):
        if self.last_token < self.first_token:
            raise ValueError("Reversed span")
        return self


class RequestSpanProposal(StrictModel):
    intent: Literal["prepare_materials", "find_institution", "unsupported"]
    outputs: list[Literal["brief", "presentation"]] = Field(max_length=2)
    institution_name: TextSpan | None
    city: TextSpan | None
    website_candidate: TextSpan | None
    meeting_goal: TextSpan | None
    meeting_date_text: TextSpan | None
    template: TextSpan | None


def request_tokens(message: str):
    """Keep offsets locally; the LLM selects numbered tokens, never invents values."""
    return list(re.finditer(r"\w+|[^\w\s]", message))


class RequestInterpreter:
    """LLM parses language; trusted server must resolve, authorize and execute."""

    def __init__(self, provider: JSONProvider):
        self.provider = provider

    async def interpret(self, request: UserRequest) -> InterpretedRequest:
        prompt = (Path(__file__).with_name("prompts") / "interpret_request.txt").read_text(
            encoding="utf-8"
        )
        tokens = request_tokens(request.message)
        if len(tokens) > 2000:
            raise ValueError("Request has too many tokens")
        schema = RequestSpanProposal.model_json_schema()
        for field in ("first_token", "last_token"):
            schema["$defs"]["TextSpan"]["properties"][field]["enum"] = list(range(len(tokens)))
        raw = await self.provider.complete(prompt, {
            "message": request.message,
            "tokens": [{"id": i, "text": token.group()} for i, token in enumerate(tokens)],
        }, schema)
        try:
            spans = RequestSpanProposal.model_validate_json(raw)
            details = {}
            for field in (
                "institution_name", "city", "website_candidate", "meeting_goal",
                "meeting_date_text", "template",
            ):
                span = getattr(spans, field)
                if span is None:
                    details[field] = None
                else:
                    if span.last_token >= len(tokens):
                        raise ValueError("Unknown token")
                    details[field] = request.message[
                        tokens[span.first_token].start():tokens[span.last_token].end()
                    ]
            proposal = RequestProposal(intent=spans.intent, outputs=spans.outputs, **details)
            if proposal.website_candidate is not None:
                HttpUrl(proposal.website_candidate)
        except (ValidationError, ValueError):
            # Never log the model response or the user message through validation errors.
            raise LLMError("invalid_request_interpretation") from None

        codes, questions = [], []
        if proposal.intent == "unsupported":
            next_step = "unsupported"
        else:
            if not (proposal.institution_name or proposal.website_candidate):
                codes.append("institution_required")
                questions.append("Укажите название учреждения и город либо адрес его сайта.")
            if proposal.intent == "prepare_materials" and not proposal.outputs:
                codes.append("output_required")
                questions.append("Подготовить справку, презентацию или оба материала?")
            next_step = "clarify" if codes else "resolve_institution"
        return InterpretedRequest(
            run=RunInfo(provider=self.provider.name, model=self.provider.model,
                        prompt_version="interpret-request-v3", created_at=datetime.now(timezone.utc)),
            request=proposal, next_step=next_step, clarification_codes=codes, questions=questions,
        )
