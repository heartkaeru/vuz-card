"""Demo 1 UI API backed by the real parser, LLM workflow and SQLite staging."""

import asyncio
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import Field, HttpUrl, field_validator

from database.demo1_store import DemoRepository
from parser.config import SectionSpec, Settings, UniversityProfile
from parser.main import UniversityParser

from app.services.llm.client import ConfigurationError, GigaChatProvider, GigaChatSettings, LLMError
from app.services.llm.models import Institution, MeetingContext, StrictModel
from app.services.llm.pipeline import AssistantPipeline, DraftValidationError
from app.services.llm.render import render_markdown
from app.services.llm.workflow import LLMWorkflow
from app.services.llm.workflow_models import ReviewBatch, WorkflowError
from app.services.llm.changes import compare_run, effective_facts
from app.services.meeting_pptx import build_meeting_presentation
from app.services.dossier_preview import build_dossier_preview

router = APIRouter(prefix="/ai", tags=["Демо 1: сбор, проверка и презентация"])


@lru_cache(maxsize=1)
def get_repository():
    default = Path(__file__).resolve().parents[3] / "output/mvp/registry.sqlite3"
    return DemoRepository(Path(os.environ.get("VUZ_MVP_DB", str(default))))


def get_pipeline():
    try:
        return AssistantPipeline(GigaChatProvider(GigaChatSettings.from_env()), chunk_size=4000,
                                 overlap=400, max_chunks=6)
    except ConfigurationError:
        raise HTTPException(503, "gigachat_configuration_required") from None


def get_collector():
    def collect(website, name, leadership_url=None, *, biography_url=None, institution_urls=(), pdf_sources=(), include_institution=False):
        settings = Settings.from_env()
        settings.max_retries = 1
        settings.timeout = min(settings.timeout, 20)
        settings.max_total_requests = min(settings.max_total_requests, 30)
        settings.fetch_item_pages = False
        settings.max_bios = min(settings.max_bios, 3)
        # A refresh must attempt the live source, not silently refresh cached dates.
        settings.use_cache = False
        parser = UniversityParser(settings)
        try:
            profile = (UniversityProfile(slug="selected-official-sources", name=name, url=website,
                       sections=(SectionSpec(name="leadership", url=leadership_url),) if leadership_url else (),
                       bio_urls=(biography_url,) if biography_url else ()) if leadership_url or biography_url else None)
            result = parser.parse(url=website, name=name, profile=profile,
                                  allowed_sections=("about", "leadership", "contacts") if include_institution or not leadership_url else ("leadership",))
            existing = {document.url for document in result.documents}
            reference_documents = parser.collect_institution_documents(institution_urls)
            if len(reference_documents) < len(institution_urls):
                result.warnings.append("Часть справочных HTML-страниц не получена. Для PDF используйте отдельное поле отчёта.")
            pdf_documents, pdf_errors = parser.collect_pdf_documents(pdf_sources)
            result.errors.extend(pdf_errors)
            reference_documents.extend(pdf_documents)
            for document in pdf_documents:
                if document.metadata.get("partial_pdf") == "true":
                    result.warnings.append(f"PDF: обработаны страницы {document.metadata['pdf_selected_pages']} из {document.metadata['pdf_page_count']}; полный отчёт не обработан.")
                if document.metadata.get("pdf_pages_without_text"):
                    result.warnings.append("pdf_pages_without_text_ocr_required")
            for document in reference_documents:
                if document.url not in existing:
                    result.documents.append(document)
                    existing.add(document.url)
            if leadership_url:
                # The homepage was used for discovery, not selected as a fact source.
                result.documents = [document for document in result.documents
                                    if document.category in {"leadership", "contacts"}
                                    or include_institution and document.section == "about"]
            if include_institution:
                from parser.institution_context import mark_institution_context
                for document in result.documents:
                    mark_institution_context(document, name)
            return result
        finally:
            parser.close()
    return collect


class CardCreate(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    city: str = Field(default="", max_length=160)
    website: HttpUrl


class PDFReference(StrictModel):
    url: HttpUrl
    pages: list[Annotated[int, Field(strict=True, ge=1, le=300)]] | None = Field(default=None, min_length=1, max_length=20)

    @field_validator("pages")
    @classmethod
    def unique_pages(cls, pages):
        return sorted(set(pages)) if pages is not None else None


class RefreshRequest(StrictModel):
    version: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=150)
    leadership_url: HttpUrl | None = None
    biography_url: HttpUrl | None = None
    institution_urls: list[HttpUrl] = Field(default_factory=list, max_length=3)
    pdf_sources: list[PDFReference] = Field(default_factory=list, max_length=2)
    include_institution: bool = False


class ReviewRequest(ReviewBatch):
    # Local prototype identity. Production must supply the actor through authentication.
    reviewer: str = Field(min_length=1, max_length=160)


class MaterialRequest(StrictModel):
    presentation_variant: Literal["compact", "detailed"] = "detailed"
    expected_revision: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=150)
    meeting: MeetingContext = Field(default_factory=MeetingContext)
    fact_ids: list[str] | None = None


def checked(operation):
    try:
        return operation()
    except WorkflowError as exc:
        code = str(exc)
        raise HTTPException(404 if code.endswith("not_found") else 409, code) from None


async def collect_job(repository, pipeline, collector, card, job_id, leadership_url=None, source_options=None):
    try:
        repository.update_job(job_id, status="running", progress=15, message="Сбор официальных страниц руководства")
        parsed = await asyncio.to_thread(collector, card["website"], card["name"], leadership_url, **(source_options or {}))
        repository.update_job(job_id, progress=55, message="Страницы получены. Извлечение фактов из текстов")
        workflow = LLMWorkflow(repository, pipeline)
        research, _ = workflow.create(Institution(id=card["id"], name=card["name"], card_version=card["version"]),
                                       parsed, job_id, modes=("0.2", "0.3") if (source_options or {}).get("include_institution") else ("0.2",))
        repository.set_latest(card["id"], research.run_id)
        research = await workflow.extract(research.run_id, context_fields=[
            "institution.profile", "institution.type", "institution.location", "institution.founded",
            "institution.education_areas", "institution.education_levels", "institution.students", "institution.structure"])
        if research.status == "failed" or not research.facts:
            stage_errors = [error.code for result in research.results.values() for error in result.errors]
            repository.update_job(job_id, status="failed", progress=100, message="Не удалось извлечь проверяемые факты",
                                  result={"run_id": research.run_id, "code": stage_errors[0] if stage_errors else "no_valid_extraction"})
        else:
            repository.update_job(job_id, status="completed", progress=100,
                message="Факты готовы к проверке" if research.status != "partial" else "Сбор завершён частично. Проверьте факты и ошибки",
                result={"run_id": research.run_id})
    except asyncio.CancelledError:
        repository.update_job(job_id, status="failed", message="Сбор прерван", result={"code": "collection_cancelled"})
        raise
    except Exception:
        repository.update_job(job_id, status="failed", message="Ошибка сбора. Повторите попытку или уточните адрес сайта",
                              result={"code": "collection_failed"})


async def material_job(repository, pipeline, run_id, payload, job_id):
    try:
        repository.update_job(job_id, status="running", progress=30, message="Подготовка тезисов по принятым фактам")
        material_id, draft = await LLMWorkflow(repository, pipeline).generate(
            run_id, payload.expected_revision, fact_ids=payload.fact_ids, meeting=payload.meeting,
            presentation_variant=payload.presentation_variant)
        repository.update_job(job_id, progress=80, message="Сборка карточки PowerPoint по шаблону")
        path = repository.path.parent / "presentations" / f"{material_id}.pptx"
        await asyncio.to_thread(build_meeting_presentation, draft, path, repository.material_facts(material_id))
        # A review or refresh during file assembly invalidates this export too.
        _, stale = repository.material(material_id)
        if stale:
            raise WorkflowError("facts_changed_during_generation")
        repository.update_job(job_id, status="completed", progress=100, message="Презентация сформирована",
            result={"material_id": material_id, "download_url": f"/api/v1/ai/materials/{material_id}/download",
                    "brief_url": f"/api/v1/ai/materials/{material_id}/brief",
                    "presentation_variant": payload.presentation_variant})
    except asyncio.CancelledError:
        repository.update_job(job_id, status="failed", message="Формирование прервано", result={"code": "generation_cancelled"})
        raise
    except (WorkflowError, DraftValidationError, LLMError) as exc:
        repository.update_job(job_id, status="failed", message="Материалы не сформированы: проверьте выбранные факты",
                              result={"code": str(exc)})
    except Exception:
        repository.update_job(job_id, status="failed", message="Ошибка формирования презентации",
                              result={"code": "material_generation_failed"})


@router.get("/cards")
def list_cards(repository=Depends(get_repository)):
    return repository.cards()


@router.post("/cards", status_code=201)
def create_card(payload: CardCreate, repository=Depends(get_repository)):
    return repository.create_card(payload.name, payload.city, str(payload.website).rstrip("/"))


@router.get("/cards/{card_id}")
def get_card(card_id: str, repository=Depends(get_repository)):
    card = checked(lambda: repository.card(card_id))
    run = repository.get(card["latest_run"]) if card["latest_run"] else None
    return {**card, "job": repository.card_job(card_id),
            "run": run.model_dump(mode="json") if run else None,
            "approved_facts": effective_facts(run) if run else []}


@router.get("/cards/{card_id}/history")
def history(card_id: str, repository=Depends(get_repository)):
    checked(lambda: repository.card(card_id))
    return [{"run_id": run.run_id, "revision": run.revision, "status": run.status,
             "created_at": run.created_at, "previous_run_id": run.previous_run_id,
             "approved_count": len(effective_facts(run)), "candidate_count": len(run.facts)}
            for run in repository.history(card_id)]


@router.post("/cards/{card_id}/refresh", status_code=202)
def refresh(card_id: str, payload: RefreshRequest, background: BackgroundTasks,
            repository=Depends(get_repository), pipeline=Depends(get_pipeline), collector=Depends(get_collector)):
    card = checked(lambda: repository.card(card_id))
    if card["version"] != payload.version:
        raise HTTPException(409, "stale_card_version")
    for source_url in [payload.leadership_url, payload.biography_url, *payload.institution_urls, *[source.url for source in payload.pdf_sources]]:
        if source_url is None:
            continue
        host = (urlsplit(card["website"]).hostname or "").removeprefix("www.")
        source_host = (source_url.host or "").removeprefix("www.")
        if source_host != host and not source_host.endswith("." + host):
            raise HTTPException(422, "leadership_source_must_belong_to_official_domain")
    source_options = {"biography_url": str(payload.biography_url) if payload.biography_url else None,
                      "institution_urls": [str(url) for url in payload.institution_urls],
                      "pdf_sources": [source.model_dump(mode="json") for source in payload.pdf_sources],
                      "include_institution": payload.include_institution or bool(payload.institution_urls or payload.pdf_sources)}
    job, created = checked(lambda: repository.new_job(card_id, payload.request_key, "collect",
        {"version": payload.version, "leadership_url": str(payload.leadership_url) if payload.leadership_url else None,
         **source_options}))
    if created:
        background.add_task(collect_job, repository, pipeline, collector, card, job["job_id"],
                            str(payload.leadership_url) if payload.leadership_url else None, source_options)
    return job


@router.get("/jobs/{job_id}")
def get_job(job_id: str, repository=Depends(get_repository)):
    return checked(lambda: repository.job(job_id))


@router.get("/runs/{run_id}")
def get_run(run_id: str, repository=Depends(get_repository)):
    return checked(lambda: repository.get(run_id))


@router.get("/runs/{run_id}/changes")
def changes(run_id: str, repository=Depends(get_repository)):
    return compare_run(checked(lambda: repository.get(run_id)))


@router.get("/runs/{run_id}/preview")
async def preview(run_id: str, expected_revision: int, presentation_variant: Literal["compact", "detailed"] = "detailed",
                  repository=Depends(get_repository)):
    run = checked(lambda: repository.check_current(run_id, expected_revision))
    facts = [f for f in run.facts if f.fact_id not in run.retired_fact_ids and f.validation_status == "passed"
             and not f.issues and f.verification_status in {"pending", "verified"}]
    facts = facts or effective_facts(run)
    if not facts:
        raise HTTPException(409, "no_previewable_facts")
    path = repository.path.parent / "previews" / f"{run_id}_{expected_revision}_{uuid4().hex}.pptx"
    await asyncio.to_thread(build_dossier_preview, run.institution, facts, path, presentation_variant=presentation_variant)
    checked(lambda: repository.check_current(run_id, expected_revision))
    return FileResponse(path, filename="Предпросмотр кандидатов — требуется проверка.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@router.post("/runs/{run_id}/reviews")
def review(run_id: str, payload: ReviewRequest, repository=Depends(get_repository)):
    return checked(lambda: LLMWorkflow(repository).review(run_id, ReviewBatch(
        expected_revision=payload.expected_revision, decisions=payload.decisions, retirements=payload.retirements), actor=payload.reviewer))


@router.post("/runs/{run_id}/materials", status_code=202)
def materials(run_id: str, payload: MaterialRequest, background: BackgroundTasks,
              repository=Depends(get_repository), pipeline=Depends(get_pipeline)):
    run = checked(lambda: repository.check_current(run_id, payload.expected_revision))
    if not effective_facts(run):
        raise HTTPException(409, "no_accepted_facts")
    job, created = checked(lambda: repository.new_job(run.institution.id, payload.request_key, "material",
                                                     {"run_id": run_id, **payload.model_dump(mode="json")}))
    if created:
        background.add_task(material_job, repository, pipeline, run_id, payload, job["job_id"])
    return job


@router.get("/materials/{material_id}/download")
def download(material_id: str, format: Literal["pptx", "docx"] = "pptx",
             repository=Depends(get_repository)):
    draft, stale = checked(lambda: repository.material(material_id))
    if stale:
        raise HTTPException(409, "material_stale_after_review")
    directory = repository.path.parent / "presentations"
    path = directory / f"{material_id}.{format}"
    if format == "docx" and not path.is_file():
        from app.services.llm.docx_export import build_word_document
        directory.mkdir(parents=True, exist_ok=True)
        build_word_document(draft, path)
        _, stale = checked(lambda: repository.material(material_id))
        if stale:
            raise HTTPException(409, "material_stale_after_review")
    if not path.is_file():
        raise HTTPException(404, "material_file_not_found")
    media_types = {
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    filename = ("Материалы к встрече.docx" if format == "docx" else
                "Краткая карточка.pptx" if draft.presentation_variant == "compact" else "Подробная справка.pptx")
    return FileResponse(path, filename=filename, media_type=media_types[format])


@router.get("/materials")
def material_list(repository=Depends(get_repository)):
    return repository.materials()


@router.get("/materials/{material_id}/brief")
def brief(material_id: str, repository=Depends(get_repository)):
    draft, stale = checked(lambda: repository.material(material_id))
    if stale:
        raise HTTPException(409, "material_stale_after_review")
    return Response(render_markdown(draft, repository.material_facts(material_id)), media_type="text/markdown; charset=utf-8")
