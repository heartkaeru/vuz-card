"""Project/event candidates grounded in parser-defined semantic scopes."""

import hashlib
import json
import re
from collections import deque
from uuid import uuid4

from pydantic import ValidationError

from .client import LLMError
from .context_models import ContextExtractionOutput, ContextExtractionRequest
from .evidence import literal_value_in_text
from .models import INSTITUTION_METRICS, EntitySubject, Evidence, ExtractionResult, Fact, FactField, PipelineError
from .names import folded


CONTEXT_SOURCE_SECTIONS = {
    "institution": ({"about", "organization", "programs", "homepage"},),
    "project": ({"projects", "research", "grants", "project_news", "partnerships"}, {"news"}),
    "event": ({"events"}, {"news", "project_news"}),
}
LINKED_CONTEXT_FIELDS = {
    "project": ("project.name", "project.role", "project.status"),
    "event": ("event.name", "event.date", "event.status"),
}
REPORT_MEASURES = {
    "institution.students": re.compile(r"(?:контингент|численност\w*)\s+(?:обучающ\w*|студент\w*)", re.I),
    "institution.teaching_staff": re.compile(r"педагогическ\w*\s+работник|профессорско-преподавательск\w*\s+состав", re.I),
    "institution.budget_students": re.compile(r"(?:за сч[её]т|средств\w*)\s+(?:федерального\s+)?бюджет", re.I),
    "institution.dormitory_capacity": re.compile(r"общежити\w*.*мест|мест.*общежити\w*", re.I),
    "institution.dormitory_occupancy": re.compile(r"общежити\w*.*(?:загруз|засел|заполн)|(?:загруз|засел|заполн).*общежити\w*", re.I),
}
INSTITUTION_NOUN = r"(?:университет|институт|академия|колледж|техникум|училище|школа|вуз|учреждение|образовательная организация)"
METRIC_TOPICS = {
    "institution.teaching_staff": r"преподавател|педагог|профессорско",
    "institution.budget_students": r"бюджет",
    "institution.dormitory_capacity": r"общежити",
    "institution.dormitory_occupancy": r"общежити",
}
FOUNDING_ACTION = r"(?:основан|учрежд[её]н|создан|образован)(?:а|о)?"
FOUNDING_CONTEXT = re.compile(
    r"\b(?:дата\s+(?:создания|основания)|год\s+основания)(?:\s+образовательной\s+организации)?\s*(?=[:|–—-]|\d)"
    rf"|\b{INSTITUTION_NOUN}\s+(?:был[ао]?\s+)?{FOUNDING_ACTION}\b"
    rf"|\b{FOUNDING_ACTION}\s+(?:(?:[а-яё-]+(?:ый|ий|ой|ая|яя|ое|ее))\s+){{0,4}}{INSTITUTION_NOUN}\b",
    re.I,
)


def normalize(value):
    return folded(" ".join(value.split()))


def context_field_groups(fields):
    by_kind = {}
    for field in fields:
        kind = field.value.partition(".")[0]
        by_kind.setdefault(kind, []).append(field)
    for kind, fields in by_kind.items():
        linked = LINKED_CONTEXT_FIELDS.get(kind, ())
        ordered = [field for key in linked for field in fields if field.value == key]
        ordered.extend(field for field in fields if field.value not in linked)
        for start in range(0, len(ordered), 3):
            yield kind, ordered[start : start + 3]


def documents_for_context_group(documents, kind, fields):
    compatible = [doc for doc in documents if doc.fields is None or set(fields).intersection(doc.fields)]
    for sections in CONTEXT_SOURCE_SECTIONS[kind]:
        selected = [doc for doc in compatible if {doc.section.lower(), doc.category.lower()} & sections]
        if selected:
            if kind == "institution":
                return [select_report_measures(doc, fields) if doc.file_sha256 else doc for doc in selected]
            return selected
    return compatible


def select_report_measures(document, fields):
    if not fields or any(field.value not in REPORT_MEASURES for field in fields):
        return document
    patterns = [REPORT_MEASURES[field.value] for field in fields]
    relevant = [fragment for fragment in document.fragments if fragment.institution_anchor_id
                and re.search(r"\d", text := normalize(document.text[fragment.start:fragment.end]))
                and any(pattern.search(text) for pattern in patterns)]
    if not relevant:
        return document
    keep = {fid for fragment in relevant for fid in (fragment.fragment_id, fragment.institution_anchor_id)}
    return document.model_copy(update={"fragments": [f for f in document.fragments if f.fragment_id in keep]})


def batches(document, size):
    """Keep one entity scope together; never silently split its title from the text."""
    anchors = {anchor for f in document.fragments
               if (anchor := f.institution_anchor_id or f.entity_anchor_id)}
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


def document_batches(request, size, *, interleave_sources):
    sources = deque()
    for document in request.documents:
        fields = request.fields
        if document.fields is not None:
            fields = document.fields if fields is None else [field for field in fields if field in document.fields]
            if not fields:
                continue
        digest = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        iterator = enumerate(batches(document, size))
        if interleave_sources:
            sources.append((document, fields, digest, iterator))
        else:
            yield document, fields, digest, iterator
    while sources:
        source = sources.popleft()
        item = next(source[3], None)
        if item is not None:
            yield (*source[:3], (item,))
            sources.append(source)


async def extract_context(
    pipeline,
    request: ContextExtractionRequest,
    *,
    progress_callback=None,
    chunk_limit: int | None = None,
    interleave_sources: bool = False,
):
    from .pipeline import PROMPTS, complete_validated

    request = ContextExtractionRequest.model_validate_json(request.model_dump_json())
    prompt = (PROMPTS / "extract_context.txt").read_text(encoding="utf-8")
    has_institution_fields = bool(request.fields and any(f.value.startswith("institution.") for f in request.fields))
    only_institution_fields = has_institution_fields and all(f.value.startswith("institution.") for f in request.fields)
    metric_requested = request.fields is not None and any(f.value in INSTITUTION_METRICS for f in request.fields)
    if has_institution_fields:
        prompt = (("Для численных показателей укажи value_fragment_id блока со значением и value как дословное "
                  "число или выражение численности из него. "
                  if metric_requested else "") +
                  "Для остальных полей institution.* выбирай один целый исходный абзац по его ID; текст перенесёт сервис. "
                  "Не собирай собственные перечисления из разных абзацев. "
                  "metric_context для численности остаётся обязательным.\n\n" + prompt)
    facts, gaps, errors, seen = [], [], [], set()
    processed = 0
    chunk_limit = pipeline.max_chunks if chunk_limit is None else max(0, chunk_limit)
    limit_reached = False
    for document, fields, digest, chunks in document_batches(
        request, pipeline.chunk_size, interleave_sources=interleave_sources,
    ):
        for index, batch in chunks:
            chunk_id = f"{document.source_id}:context:{index}"
            if sum(f.end - f.start for f in batch) > pipeline.chunk_size:
                errors.append(PipelineError(source_id=document.source_id, chunk_id=chunk_id,
                                            code="context_scope_too_large"))
                continue
            if processed >= chunk_limit:
                errors.append(PipelineError(source_id=document.source_id, chunk_id=chunk_id,
                                            code="context_batch_limit_reached"))
                limit_reached = True
                break
            processed += 1
            fragments = {f.fragment_id: f for f in batch}
            text = {f.fragment_id: document.text[f.start:f.end] for f in batch}
            payload = {
                "institution": request.institution.model_dump(mode="json"),
                "source_id": document.source_id,
                "source_title": document.title,
                "published_at": document.published_at.isoformat() if document.published_at else None,
                "fields": [f.value for f in fields] if fields is not None else None,
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
            if fields is not None:
                schema["$defs"]["ContextFactProposal"]["properties"]["field"]["enum"] = [f.value for f in fields]
            if only_institution_fields:
                definition = schema["$defs"]["ContextFactProposal"]
                if not metric_requested:
                    del definition["properties"]["value"]
                definition["properties"]["value_fragment_id"] = {"type": "string", "enum": list(fragments)}
                definition["required"].append("value_fragment_id")
            try:
                output = await complete_validated(
                    pipeline.provider,
                    ContextExtractionOutput,
                    prompt,
                    payload,
                    schema,
                )
            except (LLMError, ValidationError) as exc:
                errors.append(PipelineError(source_id=document.source_id, chunk_id=chunk_id,
                                            code=str(exc) if isinstance(exc, LLMError)
                                            else "invalid_context_json"))
                if progress_callback:
                    progress_callback(chunk_id)
                continue
            if progress_callback:
                progress_callback(chunk_id)
            gaps.extend(output.gaps)
            for item in output.facts:
                issues = []
                reference_selection = has_institution_fields and item.field.value.startswith("institution.")
                is_metric = item.field.value in INSTITUTION_METRICS
                metric_prefix = "student_metric" if item.field.value == "institution.students" else "institution_metric"
                if reference_selection and not is_metric and item.value_fragment_id is None:
                    issues.append("invalid_context_value_selection")
                if is_metric and item.value is None:
                    issues.append(f"{metric_prefix}_number_required")
                if item.value_fragment_id is not None:
                    if item.value_fragment_id not in item.fragment_ids or item.value_fragment_id not in text:
                        issues.append("invalid_context_value_selection")
                        item.value = "Неподтверждённый выбор фрагмента"
                    else:
                        selected_text = text[item.value_fragment_id]
                        if item.value is None or (reference_selection and not is_metric):
                            item.value = selected_text
                        elif not literal_value_in_text(item.value, selected_text):
                            issues.append("value_not_in_selected_fragment")
                subject = item.subject
                anchor = fragments.get(subject.entity_fragment_id)
                refs = list(dict.fromkeys([subject.entity_fragment_id, *item.fragment_ids]))
                if fields is not None and item.field not in fields:
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
                if is_metric:
                    if item.metric_context is None:
                        issues.append(f"{metric_prefix}_context_required")
                    else:
                        # All qualifiers must be supported by the very same occurrence.
                        qualified = [ref for ref in refs if ref in fragments and anchor is not None and (
                            ref == anchor.fragment_id or anchor.entity_scope_id is not None
                            and fragments[ref].entity_scope_id == anchor.entity_scope_id
                            or subject.entity_kind == "institution"
                            and fragments[ref].institution_anchor_id == anchor.fragment_id)]
                        metric_text = " ".join(text[ref] for ref in qualified)
                        if f"{metric_prefix}_number_required" not in issues and not re.search(r"\d", item.value):
                            issues.append(f"{metric_prefix}_number_required")
                        if not re.search(r"\b(?:19|20)\d{2}\b", item.metric_context.period):
                            issues.append(f"{metric_prefix}_period_required")
                        if any(not literal_value_in_text(value, metric_text)
                               for value in item.metric_context.model_dump(exclude_none=True).values()):
                            issues.append(f"{metric_prefix}_context_not_in_scope")
                        topic = METRIC_TOPICS.get(item.field.value)
                        if topic and not re.search(topic, item.metric_context.scope, re.I):
                            issues.append("institution_metric_scope_required")
                        if item.field.value == "institution.dormitory_occupancy" and not re.search(
                            r"%|процент", item.metric_context.unit, re.I,
                        ):
                            issues.append("institution_metric_unit_mismatch")
                        if item.field.value == "institution.dormitory_capacity" and not re.search(
                            r"\bмест", item.metric_context.unit, re.I,
                        ):
                            issues.append("institution_metric_unit_mismatch")
                elif item.metric_context is not None:
                    issues.append("metric_context_on_non_metric_fact")
                if item.field.value == "institution.founded":
                    if not (re.search(r"\b(?:17|18|19|20)\d{2}\b", item.value) and FOUNDING_CONTEXT.search(item.value)):
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
        if limit_reached:
            break
    # Multiple incompatible values in one source occurrence are review flags.
    groups = {}
    for fact in facts:
        if fact.validation_status == "passed" and fact.field in INSTITUTION_METRICS and fact.metric_context:
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
