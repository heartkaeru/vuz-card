"""Resolve model references to immutable source fragments and validate contact owners."""

import hashlib
import re
from uuid import uuid4

from pydantic import ValidationError

from .client import LLMError
from .fragment_models import FragmentExtractionOutput, FragmentExtractionRequest
from .models import Evidence, ExtractionResult, Fact, FactField, PipelineError, Subject, NonPersonSubject
from .names import folded, person_name_in_quote
from .evidence import literal_value_in_text

CONTACT_FIELDS = {FactField.EMAIL, FactField.PHONE}
BIOGRAPHY_FIELDS = {FactField.EDUCATION, FactField.DEGREE, FactField.TITLE, FactField.CAREER,
                    FactField.INTERESTS, FactField.RESPONSIBILITIES, FactField.APPOINTED_AT}


def normalized(value):
    return folded(" ".join(value.split()))


def value_key(field, value):
    if field == FactField.PHONE:
        digits = re.sub(r"\D", "", value)
        return "7" + digits[1:] if len(digits) == 11 and digits.startswith("8") else digits
    return normalized(value)


def batches(fragments, size):
    batch, length = [], 0
    for fragment in fragments:
        count = fragment.end - fragment.start
        if batch and length + count > size:
            yield batch
            batch, length = [], 0
        batch.append(fragment)
        length += count
    if batch:
        yield batch


async def extract_with_fragments(pipeline, request: FragmentExtractionRequest):
    from .pipeline import PROMPTS

    # Recheck offsets and scopes even if a caller mutated an existing Pydantic object.
    request = FragmentExtractionRequest.model_validate(request.model_dump(mode="python"))
    prompt = (PROMPTS / "extract_fragments.txt").read_text(encoding="utf-8")
    facts, gaps, errors, seen = [], [], [], set()
    processed = 0
    processed_fragments = set()
    for document in request.documents:
        source_hash = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        by_id = {f.fragment_id: f for f in document.fragments}
        eligible = []
        for fragment in sorted(document.fragments, key=lambda f: f.start):
            if fragment.end - fragment.start > pipeline.chunk_size:
                errors.append(PipelineError(source_id=document.source_id,
                                            chunk_id=fragment.fragment_id, code="fragment_too_large"))
            else:
                eligible.append(fragment)
        for batch in batches(eligible, pipeline.chunk_size):
            chunk_id = f"{document.source_id}:{batch[0].start}-{batch[-1].end}"
            if processed >= pipeline.max_chunks:
                errors.append(PipelineError(source_id=document.source_id, chunk_id=chunk_id,
                                            code="chunk_limit_exceeded"))
                continue
            processed += 1
            available = {f.fragment_id for f in batch}
            processed_fragments.update((document.source_id, fid) for fid in available)
            schema = FragmentExtractionOutput.model_json_schema()
            schema["$defs"]["FragmentFactProposal"]["properties"]["fragment_ids"]["items"] = {
                "type": "string", "enum": sorted(available),
            }
            payload = {
                "institution": request.institution.model_dump(mode="json"),
                "requested_fields": request.fields,
                "source_id": document.source_id,
                "fragments": [{
                    "fragment_id": f.fragment_id, "text": document.text[f.start:f.end],
                    "subject_department": f.subject_department,
                    "biography_subject": ({**f.biography_subject.model_dump(mode="json"),
                        "anchor_text": document.text[by_id[f.biography_subject.anchor_fragment_id].start:
                                                     by_id[f.biography_subject.anchor_fragment_id].end]}
                        if f.biography_subject else None),
                    "contact_hints": [h.model_dump(mode="json") for h in f.contact_hints],
                } for f in batch],
            }
            try:
                raw = await pipeline.provider.complete(prompt, payload, schema)
                output = FragmentExtractionOutput.model_validate_json(raw)
            except (ValidationError, LLMError) as exc:
                errors.append(PipelineError(source_id=document.source_id, chunk_id=chunk_id,
                                            code=str(exc) if isinstance(exc, LLMError)
                                            else "invalid_extraction_json"))
                continue
            gaps.extend(output.gaps)
            for proposal in output.facts:
                issues, refs = [], list(dict.fromkeys(proposal.fragment_ids))
                if proposal.value_fragment_id is not None:
                    chosen = by_id.get(proposal.value_fragment_id)
                    if (proposal.field not in BIOGRAPHY_FIELDS or proposal.value_fragment_id not in refs
                            or proposal.value_fragment_id not in available or chosen is None
                            or chosen.biography_subject is None):
                        issues.append("invalid_biography_value_selection")
                        proposal.value = "Неподтверждённый выбор фрагмента"
                    else:
                        proposal.value = document.text[chosen.start:chosen.end]
                if request.fields is not None and proposal.field.value not in request.fields:
                    issues.append("field_not_requested")
                if not set(refs) <= available:
                    issues.append("unknown_fragment_id")
                selected = [by_id[fid] for fid in refs if fid in available]
                context_refs = set()
                texts = [document.text[f.start:f.end] for f in selected]
                name = proposal.subject.person_name
                subject = proposal.subject.model_copy(deep=True)
                scopes = {f.subject_department for f in selected if f.subject_department
                          and name and person_name_in_quote(name, document.text[f.start:f.end])}
                if len(scopes) > 1:
                    issues.append("conflicting_subject_department_scopes")
                elif scopes:
                    scope = next(iter(scopes))
                    if subject.department and normalized(subject.department) != normalized(scope):
                        issues.append("subject_department_scope_mismatch")
                    else:
                        subject.department = scope
                if proposal.field not in CONTACT_FIELDS:
                    if not name:
                        issues.append("person_subject_required")
                    elif not any(person_name_in_quote(name, text)
                                 and (literal_value_in_text(proposal.value, text)
                                      or (proposal.field == FactField.NAME
                                          and person_name_in_quote(proposal.value, text))) for text in texts):
                        scoped = [f for f in selected if proposal.field in BIOGRAPHY_FIELDS
                                  and f.biography_subject
                                  and normalized(f.biography_subject.person_name) == normalized(name)
                                  and literal_value_in_text(proposal.value, document.text[f.start:f.end])]
                        if scoped:
                            context_refs.update(f.biography_subject.anchor_fragment_id for f in scoped)
                            refs.extend(sorted(context_refs))
                        else:
                            issues.append("person_value_not_in_same_fragment")
                    if proposal.contact_owner is not None:
                        issues.append("owner_on_non_contact_fact")
                else:
                    owner = proposal.contact_owner
                    value_fragments = [f for f in selected
                                       if literal_value_in_text(proposal.value, document.text[f.start:f.end],
                                                                email=proposal.field == FactField.EMAIL)]
                    if not value_fragments:
                        issues.append("value_not_in_fragment")
                    if owner is None or owner.kind == "unknown":
                        issues.append("contact_owner_unknown")
                    elif owner.kind == "person":
                        if not name or normalized(owner.label) != normalized(name):
                            issues.append("person_owner_subject_mismatch")
                        elif not any(person_name_in_quote(name, text) for text in texts):
                            issues.append("owner_not_in_fragment")
                    elif name is not None:
                        issues.append("non_person_contact_has_person_subject")
                    hints = [hint for f in value_fragments for hint in f.contact_hints
                             if hint.field == proposal.field.value
                             and normalized(hint.value) == normalized(proposal.value)]
                    if not hints:
                        issues.append("contact_owner_requires_source_scope")
                    elif owner is not None:
                        matching = [h for h in hints if h.owner.kind == owner.kind
                                    and normalized(h.owner.label) == normalized(owner.label)]
                        if len({(h.owner.kind, normalized(h.owner.label)) for h in hints}) > 1:
                            issues.append("conflicting_contact_scopes")
                        if not matching:
                            issues.append("contact_owner_scope_mismatch")
                        else:
                            for hint in matching:
                                refs.extend(hint.context_fragment_ids)
                if proposal.field == FactField.NAME and name and normalized(name) != normalized(proposal.value):
                    issues.append("name_context_mismatch")
                if proposal.field == FactField.INTERESTS and not re.search(
                    r"научн\w*\s+интерес|област\w*\s+(?:научн\w*\s+)?интерес|направлени\w*\s+исследован|специализиру",
                    " ".join(texts), re.I,
                ):
                    issues.append("research_interests_require_explicit_statement")
                if proposal.field == FactField.EMAIL and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", proposal.value):
                    issues.append("invalid_email")
                if proposal.field == FactField.PHONE and not 7 <= len(re.sub(r"\D", "", proposal.value)) <= 15:
                    issues.append("invalid_phone")
                evidence = []
                for fid in dict.fromkeys(refs):
                    if fid not in by_id or (fid not in available and fid not in context_refs and fid not in {
                        context_id for f in selected for hint in f.contact_hints
                        for context_id in hint.context_fragment_ids
                    }):
                        continue
                    fragment = by_id[fid]
                    evidence.append(Evidence(
                        source_id=document.source_id, quote=document.text[fragment.start:fragment.end],
                        url=str(document.url), retrieved_at=document.retrieved_at,
                        published_at=document.published_at, content_sha256=source_hash,
                        chunk_id=chunk_id, fragment_id=fid,
                        start_offset=fragment.start, end_offset=fragment.end,
                        page_number=fragment.page_number, file_sha256=document.file_sha256,
                    ))
                owner_key = (proposal.contact_owner.model_dump_json() if proposal.contact_owner else None)
                # Unit contacts repeated in rows belong to the unit, not each person.
                subject_key = subject.model_dump_json() if name else None
                key = (document.source_id, subject_key, proposal.field,
                       value_key(proposal.field, proposal.value), owner_key, tuple(sorted(set(issues))))
                if key in seen:
                    continue
                seen.add(key)
                facts.append(Fact(
                    fact_id=f"fact_{uuid4().hex}", institution_id=request.institution.id,
                    subject=subject, field=proposal.field, value=proposal.value,
                    contact_owner=proposal.contact_owner, evidence=evidence,
                    validation_status="failed" if issues else "passed",
                    verification_status="rejected" if issues else "pending", issues=sorted(set(issues)),
                ))
    # Typed, validated parser values do not need to be retyped by a language model.
    # This is opt-in and never applies to unknown owners or unprocessed fragments.
    if request.project_structured_contacts:
        contact_keys = {
            (e.source_id, f.field.value, value_key(f.field, f.value),
             f.contact_owner.kind, normalized(f.contact_owner.label))
            for f in facts if f.field in CONTACT_FIELDS and f.validation_status == "passed" and f.contact_owner
            for e in f.evidence
        }
        failed_fragments = {(error.source_id, error.chunk_id) for error in errors
                            if error.code == "fragment_too_large"}
        for document in request.documents:
            by_id = {f.fragment_id: f for f in document.fragments}
            digest = hashlib.sha256(document.text.encode()).hexdigest()
            for fragment in document.fragments:
                if (document.source_id, fragment.fragment_id) in failed_fragments:
                    continue
                if (document.source_id, fragment.fragment_id) not in processed_fragments:
                    continue
                for hint in fragment.contact_hints:
                    if (hint.owner.kind == "unknown" or
                            (request.fields is not None and hint.field not in request.fields)):
                        if hint.owner.kind == "unknown":
                            gaps.append(f"{document.source_id}/{fragment.fragment_id}: contact_owner_unknown")
                        continue
                    scopes = {(h.owner.kind, normalized(h.owner.label)) for h in fragment.contact_hints
                              if h.field == hint.field and value_key(h.field, h.value) == value_key(hint.field, hint.value)}
                    if len(scopes) > 1:
                        gaps.append(f"{document.source_id}/{fragment.fragment_id}: conflicting_contact_scopes")
                        continue
                    if hint.field == "contact.email" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", hint.value):
                        continue
                    if hint.field == "contact.phone" and not 7 <= len(re.sub(r"\D", "", hint.value)) <= 15:
                        continue
                    key = (document.source_id, hint.field, value_key(hint.field, hint.value),
                           hint.owner.kind, normalized(hint.owner.label))
                    if key in contact_keys:
                        continue
                    contact_keys.add(key)
                    refs = list(dict.fromkeys([fragment.fragment_id, *hint.context_fragment_ids]))
                    evidence = [Evidence(
                        source_id=document.source_id, quote=document.text[by_id[fid].start:by_id[fid].end],
                        url=str(document.url), retrieved_at=document.retrieved_at,
                        published_at=document.published_at, content_sha256=digest,
                        chunk_id=f"parser:{fragment.fragment_id}", fragment_id=fid,
                        start_offset=by_id[fid].start, end_offset=by_id[fid].end,
                        page_number=by_id[fid].page_number, file_sha256=document.file_sha256,
                    ) for fid in refs]
                    subject = (Subject(person_name=hint.owner.label) if hint.owner.kind == "person" else
                               NonPersonSubject(department=hint.owner.label if hint.owner.kind == "department" else None))
                    facts.append(Fact(
                        fact_id=f"fact_{uuid4().hex}", institution_id=request.institution.id,
                        subject=subject, field=hint.field, value=hint.value, contact_owner=hint.owner,
                        evidence=evidence, origin="parser_projection", validation_status="passed",
                        verification_status="pending",
                    ))
    # Derive an atomic name only from a supported person fact, never from an office label.
    named = {(normalized(f.subject.person_name), f.subject.department) for f in facts
             if f.subject.person_name and f.field == FactField.NAME and f.validation_status == "passed"}
    if request.fields is None or FactField.NAME.value in request.fields:
        for fact in list(facts):
            name = fact.subject.person_name
            key = (normalized(name), fact.subject.department) if name else None
            if not name or fact.validation_status != "passed" or key in named:
                continue
            named.add(key)
            facts.append(Fact(
                fact_id=f"fact_{uuid4().hex}", institution_id=request.institution.id,
                subject=fact.subject, field=FactField.NAME, value=name,
                evidence=fact.evidence, origin="subject_projection",
                validation_status="passed", verification_status="pending",
            ))
    # Different duties for one person are not automatically a conflict.
    heads = [f for f in facts if f.validation_status == "passed" and f.field == FactField.POSITION
             and not f.subject.department and re.fullmatch(
                 r"(?:ректор|директор|(?:и[.\s]*о[.\s]*|врио|исполняющий обязанности|"
                 r"временно исполняющий обязанности)\s*(?:ректор[а]?|директор[а]?))",
                 normalized(f.value))]
    if len({normalized(f.subject.person_name) for f in heads}) > 1:
        for fact in heads:
            fact.verification_status = "conflict"
            fact.issues.append("multiple_head_candidates")
    return ExtractionResult(
        schema_version="0.2", institution=request.institution, run=pipeline.run_info("extract-fragments-v5"),
        facts=facts, gaps=list(dict.fromkeys(gaps)), errors=errors,
        complete=not errors and all(f.validation_status == "passed" for f in facts),
    )
