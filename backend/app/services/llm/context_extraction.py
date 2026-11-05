"""Project/event candidates grounded in parser-defined semantic scopes."""

import hashlib
import json
import re
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
    anchors = {f.institution_anchor_id for f in document.fragments if f.institution_anchor_id}
    if len(anchors) == 1:
        anchor = next(f for f in document.fragments if f.fragment_id == next(iter(anchors)))
        batch, length = [anchor], anchor.end - anchor.start
        for fragment in document.fragments:
            if fragment.fragment_id == anchor.fragment_id:
                continue
            count = fragment.end - fragment.start
            if length + count > size and len(batch) > 1:
                yield batch
                batch, length = [anchor], anchor.end - anchor.start
            if length + count > size:
                yield [anchor, fragment]  # Explicitly reported as oversized, never truncated.
                continue
            batch.append(fragment)
            length += count
        if batch:
            yield batch
        return
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
    reference_selection = bool(request.fields and all(f.value.startswith("institution.") for f in request.fields))
    metric_requested = request.fields is not None and any(f.value == "institution.students" for f in request.fields)
    if reference_selection:
        prompt = (("Для institution.students возвращай value как одно дословное число или выражение численности, "
                  "а value_fragment_id не указывай. Для остальных полей ОБЯЗАТЕЛЬНО выбирай value_fragment_id без value. "
                  if metric_requested else "Для этого запроса ОБЯЗАТЕЛЬНО выбирай value_fragment_id, не возвращай value. ") +
                  "Для полей, кроме institution.students, выбирай один целый исходный абзац по его ID; текст перенесёт сервис. "
                  "Не собирай собственные перечисления из разных абзацев. "
                  "metric_context для численности остаётся обязательным.\n\n" + prompt)
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
                               "institution_anchor_id": f.institution_anchor_id,
                               "page_number": f.page_number,
                               "text": text[f.fragment_id]} for f in batch],
            }
            schema = ContextExtractionOutput.model_json_schema()
            schema["$defs"]["ContextSubjectProposal"]["properties"]["entity_fragment_id"]["enum"] = list(fragments)
            schema["$defs"]["ContextFactProposal"]["properties"]["fragment_ids"]["items"] = {
                "type": "string", "enum": list(fragments),
            }
            if request.fields is not None:
                schema["$defs"]["ContextFactProposal"]["properties"]["field"]["enum"] = [f.value for f in request.fields]
            if reference_selection and not metric_requested:
                definition = schema["$defs"]["ContextFactProposal"]
                del definition["properties"]["value"]
                definition["properties"]["value_fragment_id"] = {"type": "string", "enum": list(fragments)}
                definition["required"].append("value_fragment_id")
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
                if item.value_fragment_id is not None:
                    if item.value_fragment_id not in item.fragment_ids or item.value_fragment_id not in text:
                        issues.append("invalid_context_value_selection")
                        item.value = "Неподтверждённый выбор фрагмента"
                    else:
                        selected_text = text[item.value_fragment_id]
                        if item.value is None:
                            item.value = selected_text
                        elif not literal_value_in_text(item.value, selected_text):
                            issues.append("value_not_in_selected_fragment")
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
                    or (subject.entity_kind == "institution"
                        and fragments[ref].institution_anchor_id == anchor.fragment_id)
                    for ref in value_refs
                ):
                    issues.append("context_entity_value_scope_mismatch")
                if item.field.value == "institution.students":
                    if item.metric_context is None:
                        issues.append("student_metric_context_required")
                    else:
                        # All qualifiers must be supported by the very same occurrence.
                        qualified = [ref for ref in refs if ref in fragments and anchor is not None and (
                            ref == anchor.fragment_id or anchor.entity_scope_id is not None
                            and fragments[ref].entity_scope_id == anchor.entity_scope_id
                            or subject.entity_kind == "institution"
                            and fragments[ref].institution_anchor_id == anchor.fragment_id)]
                        metric_text = " ".join(text[ref] for ref in qualified)
                        if not re.search(r"\d", item.value):
                            issues.append("student_metric_number_required")
                        if not re.search(r"\b(?:19|20)\d{2}\b", item.metric_context.period):
                            issues.append("student_metric_period_required")
                        if any(not literal_value_in_text(value, metric_text)
                               for value in item.metric_context.model_dump(exclude_none=True).values()):
                            issues.append("student_metric_context_not_in_scope")
                elif item.metric_context is not None:
                    issues.append("metric_context_on_non_metric_fact")
                if item.field.value == "institution.founded":
                    if not (re.search(r"\b(?:17|18|19|20)\d{2}\b", item.value) and re.search(
                        r"\bдат\w*\s+(?:создания|основания)|\bгод\w*\s+основания|\b(?:основан|учрежд[её]н|создан|образован)(?:а|о|ы)?\b",
                        item.value, re.I,
                    )):
                        issues.append("institution_founding_context_required")
                if item.field.value == "institution.location" and not re.search(
                    r"\bгород|\bг\.\s*[А-ЯЁ]|адрес|местонахожд|расположен", " ".join(text[ref] for ref in value_refs), re.I,
                ):
                    issues.append("institution_location_context_required")
                if item.field.value == "institution.structure" and not re.search(
                    r"филиал|факультет|колледж|подразделен|структур|включа|входя|состав", item.value, re.I,
                ):
                    issues.append("institution_structure_context_required")
                if item.field.value == "institution.education_levels" and not re.search(
                    r"бакалавр|магистр|магистратур|специалитет|аспирант|средне\w*\s+профессиональн\w*\s+образован|дополнительн\w*\s+профессиональн\w*\s+образован",
                    item.value, re.I,
                ):
                    issues.append("education_levels_require_explicit_statement")
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
                    page_number=fragments[ref].page_number, file_sha256=document.file_sha256,
                ) for ref in refs if ref in fragments]
                key = (entity_id, item.field, normalize(item.value),
                       item.metric_context.model_dump_json() if item.metric_context else None, tuple(refs), tuple(issues))
                if key in seen:
                    continue
                seen.add(key)
                facts.append(Fact(
                    fact_id=f"fact_{uuid4().hex}", institution_id=request.institution.id,
                    subject=EntitySubject(entity_id=entity_id, entity_kind=subject.entity_kind,
                                          entity_name=subject.entity_name),
                    field=FactField(item.field.value), value=item.value, evidence=evidence,
                    metric_context=item.metric_context,
                    validation_status="failed" if issues else "passed",
                    verification_status="rejected" if issues else "pending", issues=issues,
                ))
    # Multiple incompatible values in one source occurrence are review flags.
    groups = {}
    for fact in facts:
        if fact.validation_status == "passed" and fact.field.value == "institution.students" and fact.metric_context:
            metric = fact.metric_context
            key = (fact.subject.entity_id, fact.field, normalize(metric.period), normalize(metric.scope),
                   normalize(metric.coverage or ""), normalize(metric.unit))
            groups.setdefault(key, []).append(fact)
        if fact.validation_status == "passed" and fact.field.value in {
            "project.status", "project.period", "event.date", "event.status",
        }:
            groups.setdefault((fact.subject.entity_id, fact.field), []).append(fact)
    for group in groups.values():
        if len({re.sub(r"\s", "", f.value) if re.fullmatch(r"[\d\s]+", f.value) else normalize(f.value) for f in group}) > 1:
            for fact in group:
                fact.verification_status = "conflict"
                fact.issues.append("conflicting_context_values")
    if not facts and not errors:
        gaps.append("В переданных фрагментах подтверждаемые факты профиля, проектов и событий не извлечены.")
    return ExtractionResult(
        schema_version="0.3", institution=request.institution, run=pipeline.run_info("extract-context-v3"),
        facts=facts, gaps=list(dict.fromkeys(gaps)), errors=errors,
        complete=not errors and all(f.validation_status == "passed" for f in facts),
    )
