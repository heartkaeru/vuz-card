"""Persistent LLM research, separate from legal entity/CRM identity confirmation."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .models import ExtractionResult, Fact, Institution, StrictModel


class WorkflowError(ValueError):
    """Safe machine-readable code for callers; no model/source bodies."""


class ResearchRun(StrictModel):
    run_id: str
    institution: Institution
    target_kind: Literal["registered", "draft"]
    status: Literal["queued", "running", "needs_review", "partial", "failed"] = "queued"
    revision: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime
    # Immutable extraction snapshots, distinct from reviewed copies below.
    results: dict[str, ExtractionResult] = Field(default_factory=dict)
    facts: list[Fact] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    parser_warnings: list[str] = Field(default_factory=list)
    parser_errors: list[str] = Field(default_factory=list)
    requested_modes: list[Literal["0.2", "0.3"]] = Field(default_factory=lambda: ["0.2", "0.3"])


class ReviewDecision(StrictModel):
    fact_id: str = Field(min_length=1)
    decision: Literal["accept", "reject"]
    source_checked: bool = False
    reason: str = Field(min_length=1)


class ReviewBatch(StrictModel):
    expected_revision: int = Field(ge=1)
    decisions: list[ReviewDecision] = Field(min_length=1, max_length=1000)
