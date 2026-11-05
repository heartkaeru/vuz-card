"""Extraction produces review candidates; drafting accepts only reviewed facts."""

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .client import JSONProvider, LLMError
from .context_models import ContextExtractionRequest
from .context_render import context_statement, context_title
from .names import folded, person_name_in_quote
from .contacts import contact_statement
from .leadership_render import leadership_statement
from .models import (
    DraftOutput, DraftResult, EntitySubject, Evidence, ExtractionOutput, ExtractionRequest,
    ExtractionResult, Fact, FactField, GenerationRequest, PipelineError, RunInfo,
)

PROMPTS = Path(__file__).with_name("prompts")


class DraftValidationError(ValueError):
    pass


def normalize(text: str) -> str:
    """Only whitespace is normalized for evidence matching; wording is preserved."""
    return " ".join(text.split())


def strip_reference_annotations(text: str, allowed_ids: list[str]) -> str:
    """Remove only parenthesized ID annotations already listed in structured refs.

    Unknown IDs and IDs embedded in prose are deliberately left for rejection.
    No factual words, values or source evidence are changed.
    """
    pattern = (r"\((?:fact_ids?:\s*)?(?:\[)?"
               r"(fact_[a-zA-Z0-9_]+(?:\s*[,;]\s*fact_[a-zA-Z0-9_]+)*)"
               r"(?:\])?\)")

    def replace(match):
        refs = re.split(r"\s*[,;]\s*", match.group(1))
        return "" if set(refs) <= set(allowed_ids) else match.group()

    return re.sub(r" +([.,;:])", r"\1", re.sub(pattern, replace, text)).strip()


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

    async def extract_facts(self, request: ExtractionRequest | ContextExtractionRequest) -> ExtractionResult:
        if request.schema_version == "0.3":
            from .context_extraction import extract_context
            return await extract_context(self, request)
        if request.schema_version == "0.2":
            from .fragment_extraction import extract_with_fragments
            return await extract_with_fragments(self, request)
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
                    "requested_fields": request.fields,
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
                    if request.fields is not None and proposal.field.value not in request.fields:
                        issues.append("field_not_requested")
                    for item in proposal.evidence:
                        if item.source_id != document.source_id:
                            issues.append("unknown_source_id")
                            continue
                        if normalize(item.quote) not in normalize(text):
                            issues.append("quote_not_found")
                            continue
                        # Name and literal value in quote are necessary, not sufficient,
                        # evidence of ownership; a reviewer must still check semantics.
                        if not person_name_in_quote(proposal.subject.person_name, item.quote):
                            issues.append("owner_not_in_quote")
                        if proposal.field in {
                            FactField.POSITION, FactField.EMAIL,
                            FactField.PHONE, FactField.DEPARTMENT,
                        } and folded(normalize(proposal.value)) not in folded(normalize(item.quote)):
                            issues.append("value_not_in_quote")
                        if proposal.field == FactField.NAME and not person_name_in_quote(proposal.value, item.quote):
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
                    if (proposal.field == FactField.NAME
                            and folded(proposal.value) != folded(proposal.subject.person_name)):
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
        # A validated full name already extracted into subject is also an atomic fact.
        # This projection does not create a new identity or imply manual approval.
        named = {folded(f.subject.person_name) for f in facts
                 if f.field == FactField.NAME and f.validation_status == "passed"}
        if request.fields is None or FactField.NAME.value in request.fields:
            for fact in list(facts):
                key = folded(fact.subject.person_name)
                if fact.validation_status != "passed" or key in named:
                    continue
                named.add(key)
                facts.append(Fact(
                    fact_id=f"fact_{uuid4().hex}", institution_id=request.institution.id,
                    subject=fact.subject, field=FactField.NAME, value=fact.subject.person_name,
                    evidence=fact.evidence, origin="subject_projection",
                    validation_status="passed", verification_status="pending",
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
            institution=request.institution, run=self.run_info("extract-v4"), facts=facts,
            gaps=list(dict.fromkeys(gaps)), errors=errors,
            complete=not errors and all(f.validation_status == "passed" for f in facts),
        )

    async def generate_materials(self, request: GenerationRequest) -> DraftResult:
        now = datetime.now(timezone.utc)
        ids = [fact.fact_id for fact in request.facts]
        if len(ids) != len(set(ids)):
            raise DraftValidationError("duplicate_fact_id")
        for fact in request.facts:
            if fact.field == FactField.STUDENTS and fact.metric_context is None:
                raise DraftValidationError("student_metric_context_required")
            context_field = fact.field.value.startswith(("institution.", "project.", "event."))
            if context_field or isinstance(fact.subject, EntitySubject):
                if request.schema_version != "0.3":
                    raise DraftValidationError("context_facts_require_schema_0_3")
                if (not isinstance(fact.subject, EntitySubject)
                        or not fact.field.value.startswith(fact.subject.entity_kind + ".")
                        or fact.contact_owner is not None):
                    raise DraftValidationError("context_subject_field_mismatch")
            if fact.field in {FactField.EMAIL, FactField.PHONE} and (
                request.schema_version in {"0.2", "0.3"} or fact.contact_owner is not None
                or any(e.fragment_id is not None for e in fact.evidence)
            ):
                owner = fact.contact_owner
                if owner is None or owner.kind == "unknown":
                    raise DraftValidationError("contact_owner_required")
                if owner.kind == "person":
                    if not fact.subject.person_name or folded(owner.label) != folded(fact.subject.person_name):
                        raise DraftValidationError("person_owner_subject_mismatch")
                elif fact.subject.person_name is not None:
                    raise DraftValidationError("non_person_contact_has_person_subject")
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
        has_context = any(isinstance(f.subject, EntitySubject) for f in request.facts)
        prompt = (PROMPTS / ("draft_context.txt" if has_context else "draft.txt")).read_text(encoding="utf-8")
        # Evidence may mention facts which have not been approved individually.
        # Keep it in the source registry, but do not send it to the drafting model.
        payload = request.model_dump(mode="json")
        aliases = {f"fact_{index}": fact.fact_id for index, fact in enumerate(request.facts, 1)}
        payload["facts"] = [{
            "fact_id": alias, "field": fact.field.value, "value": fact.value,
            "person_name": fact.subject.person_name,
            **({"department": fact.subject.department} if getattr(fact.subject, "department", None) else {}),
            **({"metric_context": fact.metric_context.model_dump(mode="json")} if fact.metric_context else {}),
            **({"contact_owner": fact.contact_owner.model_dump(mode="json")} if fact.contact_owner else {}),
            **({"entity": {"kind": fact.subject.entity_kind, "name": fact.subject.entity_name}}
               if isinstance(fact.subject, EntitySubject) else {}),
        } for alias, fact in zip(aliases, request.facts)]
        schema = DraftOutput.model_json_schema()
        for definition, field in (("GroundedStatement", "fact_ids"),
                                   ("MeetingQuestion", "basis_fact_ids")):
            schema["$defs"][definition]["properties"][field]["items"] = {
                "type": "string", "enum": list(aliases),
            }
        try:
            raw = await self.provider.complete(prompt, payload,
                                               schema)
            draft = DraftOutput.model_validate_json(raw)
        except ValidationError:
            raise DraftValidationError("invalid_draft_json") from None
        fact_map = {fact.fact_id: fact for fact in request.facts}
        statements = [s for section in draft.brief_sections for s in section.statements]
        theses = [s for slide in draft.slides for s in slide.theses]
        for statement in [*statements, *theses]:
            statement.text = strip_reference_annotations(statement.text, statement.fact_ids)
            statement.fact_ids = [aliases.get(fid, fid) for fid in statement.fact_ids]
        for question in draft.questions:
            question.question = strip_reference_annotations(question.question, question.basis_fact_ids)
            question.basis_fact_ids = [aliases.get(fid, fid) for fid in question.basis_fact_ids]
        if not statements or not theses:
            raise DraftValidationError("empty_material")
        if len(" ".join(s.text for s in statements).split()) > 800:
            raise DraftValidationError("brief_too_long")
        for part in [*draft.brief_sections, *draft.slides]:
            self._validate_references(part.title, ids, fact_map)
        for statement in [*statements, *theses]:
            self._validate_references(statement.text, statement.fact_ids, fact_map)
            if request.schema_version in {"0.2", "0.3"}:
                canonical = contact_statement([fact_map[fid] for fid in statement.fact_ids])
                if canonical is not None:
                    statement.text = canonical
                canonical = leadership_statement([fact_map[fid] for fid in statement.fact_ids])
                if canonical is not None:
                    statement.text = canonical
            selected = [fact_map[fid] for fid in statement.fact_ids]
            if any(isinstance(f.subject, EntitySubject) for f in selected):
                canonical = context_statement(selected)
                if canonical is None:
                    raise DraftValidationError("context_statement_requires_single_entity")
                statement.text = canonical
        if len(" ".join(s.text for s in statements).split()) > 800:
            raise DraftValidationError("brief_too_long")
        for part in [*draft.brief_sections, *draft.slides]:
            items = part.statements if hasattr(part, "statements") else part.theses
            unique, seen = [], set()
            for item in items:
                key = (item.text, tuple(sorted(set(item.fact_ids))))
                if key not in seen:
                    seen.add(key)
                    unique.append(item)
            if hasattr(part, "statements"):
                part.statements = unique
            else:
                part.theses = unique
            items = unique
            selected = [fact_map[fid] for s in items for fid in s.fact_ids]
            canonical_title = context_title(selected)
            if canonical_title is not None:
                part.title = canonical_title
        for question in draft.questions:
            self._validate_references(question.question, question.basis_fact_ids, fact_map)
        return DraftResult(
            **draft.model_dump(), schema_version=request.schema_version,
            institution=request.institution, meeting=request.meeting,
            run=self.run_info("draft-context-v2" if has_context else "draft-v5"),
            sources={fid: f.evidence for fid, f in fact_map.items()},
            warnings=["Черновик: требуется проверка сотрудником. Ссылки и числа проверены "
                      "автоматически; смысловая подтверждённость всего текста не гарантируется."],
        )

    @staticmethod
    def _validate_references(text: str, ids: list[str], facts: dict[str, Fact]):
        if re.search(r"\bfact_[a-zA-Z0-9_]+\b", text):
            raise DraftValidationError("internal_fact_id_in_text")
        if not set(ids) <= facts.keys():
            raise DraftValidationError("unknown_fact_reference")
        # Numeric checks are deliberately narrow, not a semantic entailment claim.
        allowed = " ".join(facts[fid].value + " " + (
            " ".join(facts[fid].metric_context.model_dump(exclude_none=True).values()) if facts[fid].metric_context else "") for fid in ids)
        if not set(re.findall(r"\d+", text)) <= set(re.findall(r"\d+", allowed)):
            raise DraftValidationError("unsupported_number")
