"""Parser -> durable candidates -> explicit review -> draft from stored facts."""

import asyncio
import json
from uuid import uuid4

from pydantic import ValidationError

from database.llm_store import LLMRepository, stamp
from parser.llm_adapter import to_llm_request
from parser.models import UniversityData

from .client import LLMError
from .fragment_models import parse_extraction_request
from .models import GenerationRequest, Institution, MeetingContext
from .pipeline import AssistantPipeline, DraftValidationError
from .workflow_models import ResearchRun, ReviewBatch, WorkflowError
from .changes import compare_run, effective_facts, head_key, normalized


def documents_for(card, mode):
    result = []
    for document in card.documents:
        labels = {document.category, document.section}
        leadership = bool(labels & {"leadership", "contacts", "biography"})
        context = bool(labels & {"organization", "about", "homepage", "research", "projects",
                                "grants", "project_news", "partnerships", "achievements", "events", "news"})
        unknown = not leadership and not context
        hints = any(f.contact_hints for f in document.fragments)
        if (mode == "0.2" and (leadership or hints or unknown)
                or mode == "0.3" and (context or unknown)):
            result.append(document)
    if mode == "0.2":
        result.sort(key=lambda d: 0 if d.section == "leadership" else
                    1 if d.category == "leadership" else 2 if d.category == "contacts" else 3)
    else:
        # Explicit report pages take priority over auto-discovered general pages.
        result.sort(key=lambda d: 0 if d.file_sha256 else 1)
    return result


class LLMWorkflow:
    def __init__(self, repository: LLMRepository, pipeline: AssistantPipeline | None = None):
        self.repository = repository
        self.pipeline = pipeline

    def create(self, institution: Institution, card: UniversityData, request_key: str,
               modes=("0.2", "0.3")):
        if not modes or not set(modes) <= {"0.2", "0.3"}:
            raise WorkflowError("unsupported_extraction_modes")
        # Parser model instances can be mutated; validate a fresh immutable snapshot.
        card = UniversityData.model_validate_json(card.model_dump_json())
        return self.repository.create(institution, card.model_dump(mode="json"), request_key,
                                      tuple(dict.fromkeys(modes)))

    async def extract(self, run_id: str, *, context_fields=None) -> ResearchRun:
        if self.pipeline is None:
            raise WorkflowError("llm_provider_required")
        run = self.repository.get(run_id)
        if run.status != "queued":
            raise WorkflowError("run_already_started_use_new_request_key_for_retry")

        def start(current, db):
            if current.status != "queued":
                raise WorkflowError("run_already_started")
            current.status = "running"

        run = self.repository.mutate(run_id, run.revision, start)
        card = UniversityData.model_validate(self.repository.source_card(run_id))
        results, errors, cancelled = {}, [], False
        try:
            for mode in run.requested_modes:
                selected = documents_for(card, mode)
                usable = [d for d in selected if d.text.strip() and d.fragments]
                if len(usable) != len(selected):
                    errors.append(f"{mode}:empty_source_documents")
                selected = usable
                if not selected:
                    errors.append(f"{mode}:no_source_documents")
                    continue
                stage_card = card.model_copy(update={"documents": selected})
                payload = to_llm_request(stage_card, institution_id=run.institution.id,
                                         card_version=run.institution.card_version, schema_version=mode)
                payload["institution"] = run.institution.model_dump(mode="json")
                if mode == "0.2":
                    # Proven parser contacts remain candidates even if the LLM omits one.
                    payload["project_structured_contacts"] = True
                elif context_fields is not None:
                    payload["fields"] = context_fields
                try:
                    request = parse_extraction_request(json.dumps(payload, ensure_ascii=False))
                    results[mode] = await self.pipeline.extract_facts(request)
                except ValidationError:
                    errors.append(f"{mode}:invalid_parser_llm_contract")
                except LLMError as exc:
                    errors.append(f"{mode}:{exc}")
        except asyncio.CancelledError:
            cancelled = True
            errors.append("extraction_cancelled")
        except Exception:
            # Keep the source snapshot and failure; do not expose source/SDK details.
            errors.append("unexpected_extraction_error")
        try:
            self.repository.check_current(run_id, run.revision)
        except WorkflowError as exc:
            errors.append(str(exc))

        def finish(current, db):
            current.results = results
            current.facts = [f.model_copy(deep=True) for result in results.values() for f in result.facts]
            current.errors = errors
            partial = bool(errors or card.errors or any(not r.complete for r in results.values()))
            if not results or not current.facts:
                current.status = "failed"
            else:
                current.status = "partial" if partial else "needs_review"

        result = self.repository.mutate(run_id, run.revision, finish, check_target=False)
        if cancelled:
            raise asyncio.CancelledError
        return result

    def review(self, run_id: str, batch: ReviewBatch, *, actor: str):
        actor = actor.strip()
        if not actor:
            raise WorkflowError("review_actor_required")
        batch = ReviewBatch.model_validate_json(batch.model_dump_json())
        ids = [decision.fact_id for decision in batch.decisions]
        if len(ids) != len(set(ids)):
            raise WorkflowError("duplicate_review_fact_id")

        def apply(run, db):
            if run.status not in {"needs_review", "partial"}:
                raise WorkflowError("run_not_reviewable")
            by_id = {f.fact_id: f for f in run.facts}
            if not set(ids) <= by_id.keys():
                raise WorkflowError("unknown_review_fact_id")
            retirement_ids = [decision.fact_id for decision in batch.retirements]
            if len(retirement_ids) != len(set(retirement_ids)) or set(retirement_ids) & set(ids):
                raise WorkflowError("invalid_retirement_fact_ids")
            accepted_ids = {d.fact_id for d in batch.decisions if d.decision == "accept"}
            changes = compare_run(run)["entries"]
            active = {f.fact_id: f for f in effective_facts(run)}
            active_heads = {normalized(f.subject.person_name) for f in active.values() if head_key(f)}
            for fid in accepted_ids:
                candidate = by_id[fid]
                if (head_key(candidate) and active_heads
                        and normalized(candidate.subject.person_name) not in active_heads
                        and not any(c["kind"] == "head_change" and fid in c["retirement_trigger_ids"] for c in changes)):
                    raise WorkflowError("ambiguous_head_replacement")
            required = set()
            for change in changes:
                chosen = [f for f in change["after"] if f.fact_id in accepted_ids]
                if chosen and (change["kind"] != "head_change" or any(head_key(f) for f in chosen)):
                    required.update(change["retirement_ids"])
            if set(retirement_ids) != required:
                raise WorkflowError("explicit_replacement_confirmation_required")
            for retirement in batch.retirements:
                if retirement.fact_id not in active or not retirement.source_checked:
                    raise WorkflowError("replacement_source_check_required")
            now = stamp()
            for decision in batch.decisions:
                fact = by_id[decision.fact_id]
                if decision.decision == "accept":
                    if fact.validation_status != "passed" or fact.issues or fact.verification_status == "conflict":
                        raise WorkflowError("resolve_conflict_or_validation_before_acceptance")
                    if not decision.source_checked or not fact.evidence:
                        raise WorkflowError("source_check_required")
                before = fact.model_dump_json()
                fact.verification_status = "verified" if decision.decision == "accept" else "rejected"
                fact.reviewed_by, fact.reviewed_at = actor, now
                if decision.decision == "accept":
                    fact.last_checked_at, fact.freshness_status = now, "within_window"
                db.execute("INSERT INTO llm_review VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    f"review_{uuid4().hex}", run_id, run.revision + 1, fact.fact_id,
                    decision.decision, actor, now.isoformat(), decision.reason, before, fact.model_dump_json()))
            for retirement in batch.retirements:
                fact = active[retirement.fact_id]
                run.retired_fact_ids.append(fact.fact_id)
                after = {"fact": fact.model_dump(mode="json"), "active": False,
                         "replaced_by": sorted(accepted_ids)}
                db.execute("INSERT INTO llm_review VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    f"review_{uuid4().hex}", run_id, run.revision + 1, fact.fact_id,
                    "retire", actor, now.isoformat(), retirement.reason, fact.model_dump_json(), json.dumps(after, ensure_ascii=False)))

        return self.repository.mutate(run_id, batch.expected_revision, apply)

    async def generate(self, run_id: str, expected_revision: int, *, fact_ids=None,
                       meeting: MeetingContext | None = None, presentation_variant="detailed"):
        if presentation_variant not in {"compact", "detailed"}:
            raise WorkflowError("unsupported_presentation_variant")
        if self.pipeline is None:
            raise WorkflowError("llm_provider_required")
        run = self.repository.check_current(run_id, expected_revision)
        if run.status not in {"needs_review", "partial", "failed"}:
            raise WorkflowError("run_not_ready_for_generation")
        by_id = {f.fact_id: f for f in effective_facts(run)}
        if fact_ids is None:
            selected = list(by_id.values())
        else:
            known = {f.fact_id for f in [*run.baseline_facts, *run.facts]}
            if fact_ids and set(fact_ids) <= known and not set(fact_ids) <= by_id.keys():
                raise DraftValidationError("only_reviewed_active_facts_are_allowed")
            if not fact_ids or len(fact_ids) != len(set(fact_ids)) or not set(fact_ids) <= by_id.keys():
                raise WorkflowError("invalid_selected_fact_ids")
            selected = [by_id[fid] for fid in fact_ids]
        if not selected:
            raise WorkflowError("no_accepted_facts")
        request = GenerationRequest(schema_version="0.3", institution=run.institution,
                                    facts=selected, meeting=meeting or MeetingContext())
        draft = await self.pipeline.generate_materials(request)
        draft.presentation_variant = presentation_variant
        if run.target_kind == "draft":
            draft.warnings.append("Черновое исследование: личность и реквизиты учреждения не подтверждены в реестре CRM.")
        if run.status == "partial":
            draft.warnings.append("Частичный сбор: ошибки и пропуски исходного исследования сохранены; полнота не подтверждена.")
        if run.status == "failed":
            draft.warnings.append("Последний сбор не удался. Использованы ранее принятые сведения с исходными датами проверки; актуальность новым сбором не подтверждена.")
        material_id = self.repository.save_material(run_id, expected_revision, draft)
        return material_id, draft
