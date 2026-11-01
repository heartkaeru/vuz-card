"""Project/event candidates grounded in parser-defined semantic scopes."""

import hashlib
import json
from uuid import uuid4

from pydantic import ValidationError

from .client import LLMError
from .context_models import ContextExtractionOutput, ContextExtractionRequest
from .evidence import literal_value_in_text
from .models import EntitySubject, Evidence, ExtractionResult, Fact, FactField, PipelineError
from .names import folded


def normalize(value):
    return folded(" ".join(value.split()))


def batches(document, size):
    """Keep one entity scope together; never silently split its title from the text."""
    scopes = {}
    for fragment in document.fragments:
        key = ("scope", fragment.entity_scope_id) if fragment.entity_scope_id else ("fragment", fragment.fragment_id)
        scopes.setdefault(key, []).append(fragment)
    batch, length = [], 0
    for group in scopes.values():
        count = sum(f.end - f.start for f in group)
        if batch and length + count > size:
            yield batch
            batch, length = [], 0
        if count > size:
            # The caller records this as an explicit oversized-scope error.
            if batch:
                yield batch
                batch, length = [], 0
            yield group
            continue
        batch.extend(group)
        length += count
    if batch:
        yield batch


async def extract_context(pipeline, request: ContextExtractionRequest):
    from .pipeline import PROMPTS

    request = ContextExtractionRequest.model_validate_json(request.model_dump_json())
    prompt = (PROMPTS / "extract_context.txt").read_text(encoding="utf-8")
    facts, gaps, errors, seen = [], [], [], set()
    processed = 0
    for document in request.documents:
        digest = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        for index, batch in enumerate(batches(document, pipeline.chunk_size)):
            chunk_id = f"{document.source_id}:context:{index}"
            if processed >= pipeline.max_chunks:
                errors.append(PipelineError(source_id=document.source_id, chunk_id=chunk_id,
                                            code="context_batch_limit_reached"))
                continue
            processed += 1
            if sum(f.end - f.start for f in batch) > pipeline.chunk_size:
                errors.append(PipelineError(source_id=document.source_id, chunk_id=chunk_id,
                                            code="context_scope_too_large"))
                continue
            fragments = {f.fragment_id: f for f in batch}
            text = {f.fragment_id: document.text[f.start:f.end] for f in batch}
            payload = {
                "institution": request.institution.model_dump(mode="json"),
                "source_id": document.source_id,
                "source_title": document.title,
                "published_at": document.published_at.isoformat() if document.published_at else None,
                "fields": [f.value for f in request.fields] if request.fields is not None else None,
                "fragments": [{"fragment_id": f.fragment_id, "entity_scope_id": f.entity_scope_id,
                               "text": text[f.fragment_id]} for f in batch],
            }
            schema = ContextExtractionOutput.model_json_schema()
            schema["$defs"]["ContextSubjectProposal"]["properties"]["entity_fragment_id"]["enum"] = list(fragments)
            schema["$defs"]["ContextFactProposal"]["properties"]["fragment_ids"]["items"] = {
                "type": "string", "enum": list(fragments),
            }
            try:
                raw = await pipeline.provider.complete(prompt, payload, schema)
                output = ContextExtractionOutput.model_validate_json(raw)
            except (LLMError, ValidationError) as exc:
                errors.append(PipelineError(source_id=document.source_id, chunk_id=chunk_id,
                                            code=str(exc) if isinstance(exc, LLMError)
                                            else "invalid_context_json"))
                continue
            gaps.extend(output.gaps)
            for item in output.facts:
                issues = []
                subject = item.subject
                anchor = fragments.get(subject.entity_fragment_id)
                refs = list(dict.fromkeys([subject.entity_fragment_id, *item.fragment_ids]))
                if request.fields is not None and item.field not in request.fields:
                    issues.append("field_not_requested")
                if not item.field.value.startswith(subject.entity_kind + "."):
                    issues.append("context_entity_field_mismatch")
                if any(ref not in fragments for ref in refs):
                    issues.append("unknown_fragment_id")
                if anchor is not None and not literal_value_in_text(subject.entity_name, text[anchor.fragment_id]):
                    issues.append("context_entity_not_in_anchor")
                if item.field.value.endswith(".name") and normalize(item.value) != normalize(subject.entity_name):
                    issues.append("context_name_mismatch")
                value_refs = [ref for ref in item.fragment_ids if ref in text
                              and literal_value_in_text(item.value, text[ref])]
                if not value_refs:
                    issues.append("value_not_in_fragment")
                elif anchor is not None and not any(
                    ref == anchor.fragment_id
                    or (anchor.entity_scope_id is not None
                        and fragments[ref].entity_scope_id == anchor.entity_scope_id)
                    for ref in value_refs
                ):
                    issues.append("context_entity_value_scope_mismatch")
                # Names alone are never treated as proof of participation/organizing.
                scope = (("scope", anchor.entity_scope_id) if anchor and anchor.entity_scope_id
                         else ("fragment", subject.entity_fragment_id))
                entity_key = [request.institution.id, document.source_id, digest, scope,
                              subject.entity_kind, normalize(subject.entity_name)]
                entity_id = "entity_" + hashlib.sha256(json.dumps(entity_key, ensure_ascii=False).encode()).hexdigest()[:24]
                evidence = [Evidence(
                    source_id=document.source_id, quote=text[ref], url=str(document.url),
                    retrieved_at=document.retrieved_at, published_at=document.published_at,
                    content_sha256=digest, chunk_id=chunk_id, fragment_id=ref,
                    start_offset=fragments[ref].start, end_offset=fragments[ref].end,
                ) for ref in refs if ref in fragments]
                key = (entity_id, item.field, normalize(item.value), tuple(refs), tuple(issues))
                if key in seen:
                    continue
                seen.add(key)
                facts.append(Fact(
                    fact_id=f"fact_{uuid4().hex}", institution_id=request.institution.id,
                    subject=EntitySubject(entity_id=entity_id, entity_kind=subject.entity_kind,
                                          entity_name=subject.entity_name),
                    field=FactField(item.field.value), value=item.value, evidence=evidence,
                    validation_status="failed" if issues else "passed",
                    verification_status="rejected" if issues else "pending", issues=issues,
                ))
    # Multiple incompatible values in one source occurrence are review flags.
    groups = {}
    for fact in facts:
        if fact.validation_status == "passed" and fact.field.value in {
            "project.status", "project.period", "event.date", "event.status",
        }:
            groups.setdefault((fact.subject.entity_id, fact.field), []).append(fact)
    for group in groups.values():
        if len({normalize(f.value) for f in group}) > 1:
            for fact in group:
                fact.verification_status = "conflict"
                fact.issues.append("conflicting_context_values")
    if not facts and not errors:
        gaps.append("В переданных фрагментах подтверждаемые факты профиля, проектов и событий не извлечены.")
    return ExtractionResult(
        schema_version="0.3", institution=request.institution, run=pipeline.run_info("extract-context-v1"),
        facts=facts, gaps=list(dict.fromkeys(gaps)), errors=errors,
        complete=not errors and all(f.validation_status == "passed" for f in facts),
    )
