"""LLM extraction and grounded drafting, independent of CRM storage."""

from .pipeline import AssistantPipeline
from .requests import RequestInterpreter

__all__ = ["AssistantPipeline", "RequestInterpreter"]
