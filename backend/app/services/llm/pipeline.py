"""Extraction produces review candidates; drafting accepts only reviewed facts."""

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .client import JSONProvider, LLMError
from .models import (
    DraftOutput, DraftResult, Evidence, ExtractionOutput, ExtractionRequest,
    ExtractionResult, Fact, FactField, GenerationRequest, PipelineError, RunInfo,
)

PROMPTS = Path(__file__).with_name("prompts")


class DraftValidationError(ValueError):
    pass


def normalize(text: str) -> str:
    """Only whitespace is normalized for evidence matching; wording is preserved."""
    return " ".join(text.split())


def chunks(text: str, size: int, overlap: int):
    """Bounded chunks with original offsets; overlap helps preserve nearby context."""
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary != -1:
                end = boundary + 1
        yield start, end, text[start:end]
        if end == len(text):
            break
        start = end - overlap


class AssistantPipeline:
    def __init__(self, provider: JSONProvider, *, chunk_size: int = 12000,
                 overlap: int = 1000, max_chunks: int = 50, review_window_days: int = 7):
        if (chunk_size < 100 or not 0 <= overlap < chunk_size // 2
                or max_chunks < 1 or review_window_days < 1):
            raise ValueError("Invalid chunk limits")
        self.provider = provider
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.max_chunks = max_chunks
        self.review_window_days = review_window_days

    def run_info(self, prompt_version: str) -> RunInfo:
        return RunInfo(provider=self.provider.name, model=self.provider.model,
                       prompt_version=prompt_version, created_at=datetime.now(timezone.utc))

    async def extract_facts(self, request: ExtractionRequest) -> ExtractionResult:
        prompt = (PROMPTS / "extract.txt").read_text(encoding="utf-8")
        facts, errors, gaps = [], [], []
        seen = set()
        processed = 0
        for document in request.documents:
            digest = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
            for start, end, text in chunks(document.text, self.chunk_size, self.overlap):
                chunk_id = f"{document.source_id}:{start}-{end}"
                if processed >= self.max_chunks:
                    errors.append(PipelineError(source_id=document.source_id,
                                                chunk_id=chunk_id, code="chunk_limit_exceeded"))
                    break
                processed += 1
                payload = {
                    "institution": request.institution.model_dump(mode="json"),
                    "document": {"source_id": document.source_id, "title": document.title,
                                 "chunk_id": chunk_id, "text": text},
                }
                try:
                    raw = await self.provider.complete(prompt, payload, ExtractionOutput.model_json_schema())
                    output = ExtractionOutput.model_validate_json(raw)
                except (ValidationError, LLMError) as exc:
                    errors.append(PipelineError(source_id=document.source_id, chunk_id=chunk_id,
                                                code=str(exc) if isinstance(exc, LLMError)
                                                else "invalid_extraction_json"))
                    continue
                gaps.extend(output.gaps)
                for proposal in output.facts:
                    issues, evidence = [], []
                    for item in proposal.evidence:
                        if item.source_id != document.source_id:
                            issues.append("unknown_source_id")
                            continue
                        if normalize(item.quote) not in normalize(text):
                            issues.append("quote_not_found")
                            continue
                        # Name and literal value in quote are necessary, not sufficient,
                        # evidence of ownership; a reviewer must still check semantics.
                        if normalize(proposal.subject.person_name) not in normalize(item.quote):
                            issues.append("owner_not_in_quote")
                        if proposal.field in {
                            FactField.NAME, FactField.POSITION, FactField.EMAIL,
                            FactField.PHONE, FactField.DEPARTMENT,
                        } and normalize(proposal.value) not in normalize(item.quote):
                            issues.append("value_not_in_quote")
                        evidence.append(Evidence(
                            **item.model_dump(), url=str(document.url),
                            retrieved_at=document.retrieved_at, published_at=document.published_at,
                            content_sha256=digest, chunk_id=chunk_id,
                        ))
                    if proposal.field == FactField.EMAIL and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", proposal.value):
                        issues.append("invalid_email")
                    if proposal.field == FactField.PHONE and not 7 <= len(re.sub(r"\D", "", proposal.value)) <= 15:
                        issues.append("invalid_phone")
                    if proposal.field == FactField.NAME and proposal.value != proposal.subject.person_name:
                        issues.append("name_context_mismatch")
                    key = (document.source_id, proposal.subject.model_dump_json(), proposal.field,
                           proposal.value, tuple((e.source_id, e.quote) for e in evidence), tuple(issues))
                    if key in seen:
                        continue
                    seen.add(key)
                    facts.append(Fact(
                        fact_id=f"fact_{uuid4().hex}", institution_id=request.institution.id,
                        subject=proposal.subject, field=proposal.field, value=proposal.value,
                        evidence=evidence, issues=sorted(set(issues)),
                        validation_status="failed" if issues else "passed",
                        verification_status="rejected" if issues else "pending",
                    ))
        # Potential conflicts are conservative review flags, never identity merges.
        groups = {}
        for fact in facts:
            if fact.validation_status != "passed" or fact.field not in {
                FactField.POSITION, FactField.STATUS, FactField.DEPARTMENT,
            }:
                continue
            key = (normalize(fact.subject.person_name), fact.field)
            groups.setdefault(key, []).append(fact)
        for group in groups.values():
            if len({normalize(f.value) for f in group}) > 1:
                for fact in group:
                    fact.verification_status = "conflict"
                    fact.issues.append("possible_identity_or_value_conflict")
        # Different people presented as rector/director also require manual resolution.
        leaders = [f for f in facts if f.validation_status == "passed"
                   and f.field == FactField.POSITION
                   and not f.subject.department
                   and re.fullmatch(r"(?:(?:и\.?\s*о\.?\s+)?(?:ректор|директор)"
                                    r"|исполняющий обязанности (?:ректора|директора))", f.value, re.I)]
        if len({f.subject.person_name for f in leaders}) > 1:
            for fact in leaders:
                fact.verification_status = "conflict"
                fact.issues.append("multiple_head_candidates")
        return ExtractionResult(
            institution=request.institution, run=self.run_info("extract-v1"), facts=facts,
            gaps=list(dict.fromkeys(gaps)), errors=errors,
            complete=not errors and all(f.validation_status == "passed" for f in facts),
        )

    async def generate_materials(self, request: GenerationRequest) -> DraftResult:
        now = datetime.now(timezone.utc)
        ids = [fact.fact_id for fact in request.facts]
        if len(ids) != len(set(ids)):
            raise DraftValidationError("duplicate_fact_id")
        for fact in request.facts:
            if (fact.institution_id != request.institution.id
                    or fact.validation_status != "passed" or fact.verification_status != "verified"
                    or fact.issues or not fact.evidence or not fact.reviewed_by
                    or not fact.reviewed_at or fact.reviewed_at.utcoffset() is None
                    or fact.reviewed_at > now):
                raise DraftValidationError("only_reviewed_facts_from_this_institution_are_allowed")
            # Pilot pre-meeting window. Historical data still needs a recorded check.
            checked = fact.last_checked_at
            if (fact.freshness_status != "within_window" or checked is None
                    or checked.utcoffset() is None or checked > now
                    or now - checked > timedelta(days=self.review_window_days)):
                raise DraftValidationError("facts_require_freshness_check")
        prompt = (PROMPTS / "draft.txt").read_text(encoding="utf-8")
        try:
            raw = await self.provider.complete(prompt, request.model_dump(mode="json"),
                                               DraftOutput.model_json_schema())
            draft = DraftOutput.model_validate_json(raw)
        except ValidationError:
            raise DraftValidationError("invalid_draft_json") from None
        fact_map = {fact.fact_id: fact for fact in request.facts}
        statements = [s for section in draft.brief_sections for s in section.statements]
        theses = [s for slide in draft.slides for s in slide.theses]
        if not statements or not theses:
            raise DraftValidationError("empty_material")
        if len(" ".join(s.text for s in statements).split()) > 800:
            raise DraftValidationError("brief_too_long")
        for part in [*draft.brief_sections, *draft.slides]:
            self._validate_references(part.title, ids, fact_map)
        for statement in [*statements, *theses]:
            self._validate_references(statement.text, statement.fact_ids, fact_map)
        for question in draft.questions:
            self._validate_references(question.question, question.basis_fact_ids, fact_map)
        return DraftResult(
            **draft.model_dump(), institution=request.institution, meeting=request.meeting,
            run=self.run_info("draft-v1"), sources={fid: f.evidence for fid, f in fact_map.items()},
            warnings=["Черновик: требуется проверка сотрудником. Ссылки и числа проверены "
                      "автоматически; смысловая подтверждённость всего текста не гарантируется."],
        )

    @staticmethod
    def _validate_references(text: str, ids: list[str], facts: dict[str, Fact]):
        if not set(ids) <= facts.keys():
            raise DraftValidationError("unknown_fact_reference")
        # Numeric checks are deliberately narrow, not a semantic entailment claim.
        allowed = " ".join(facts[fid].value for fid in ids)
        if not set(re.findall(r"\d+", text)) <= set(re.findall(r"\d+", allowed)):
            raise DraftValidationError("unsupported_number")
