"""Demo 1 UI API backed by the real parser, LLM workflow and SQLite staging."""

import asyncio
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import Field, HttpUrl

from database.demo1_store import DemoRepository
from parser.config import SectionSpec, Settings, UniversityProfile
from parser.main import UniversityParser

from app.services.llm.client import ConfigurationError, GigaChatProvider, GigaChatSettings, LLMError
from app.services.llm.models import Institution, MeetingContext, StrictModel
from app.services.llm.pipeline import AssistantPipeline, DraftValidationError
from app.services.llm.render import render_markdown
from app.services.llm.workflow import LLMWorkflow
from app.services.llm.workflow_models import ReviewBatch, WorkflowError
from app.services.meeting_pptx import build_meeting_presentation

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
    def collect(website, name, leadership_url=None):
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
            profile = (UniversityProfile(slug="selected-leadership-source", name=name, url=website,
                       sections=(SectionSpec(name="leadership", url=leadership_url),)) if leadership_url else None)
            result = parser.parse(url=website, name=name, profile=profile,
                                  allowed_sections=("leadership",) if leadership_url else ("about", "leadership", "contacts"))
            if leadership_url:
                # The homepage was used for discovery, not selected as a fact source.
                result.documents = [document for document in result.documents
                                    if document.category in {"leadership", "contacts"}]
            return result
        finally:
            parser.close()
    return collect


class CardCreate(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    city: str = Field(default="", max_length=160)
    website: HttpUrl


class RefreshRequest(StrictModel):
    version: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=150)
    leadership_url: HttpUrl | None = None


class ReviewRequest(ReviewBatch):
    # Local prototype identity. Production must supply the actor through authentication.
    reviewer: str = Field(min_length=1, max_length=160)


class MaterialRequest(StrictModel):
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


async def collect_job(repository, pipeline, collector, card, job_id, leadership_url=None):
    try:
        repository.update_job(job_id, status="running", progress=15, message="Сбор официальных страниц руководства")
        parsed = await asyncio.to_thread(collector, card["website"], card["name"], leadership_url)
        repository.update_job(job_id, progress=55, message="Страницы получены. Извлечение фактов из текстов")
        workflow = LLMWorkflow(repository, pipeline)
        research, _ = workflow.create(Institution(id=card["id"], name=card["name"], card_version=card["version"]),
                                       parsed, job_id, modes=("0.2",))
        repository.set_latest(card["id"], research.run_id)
        research = await workflow.extract(research.run_id)
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
            run_id, payload.expected_revision, fact_ids=payload.fact_ids, meeting=payload.meeting)
        repository.update_job(job_id, progress=80, message="Сборка карточки PowerPoint по шаблону")
        path = repository.path.parent / "presentations" / f"{material_id}.pptx"
        await asyncio.to_thread(build_meeting_presentation, draft, path, repository.material_facts(material_id))
        # A review or refresh during file assembly invalidates this export too.
        _, stale = repository.material(material_id)
        if stale:
            raise WorkflowError("facts_changed_during_generation")
        repository.update_job(job_id, status="completed", progress=100, message="Презентация сформирована",
            result={"material_id": material_id, "download_url": f"/api/v1/ai/materials/{material_id}/download",
                    "brief_url": f"/api/v1/ai/materials/{material_id}/brief"})
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
    return {**card, "job": repository.card_job(card_id),
            "run": repository.get(card["latest_run"]).model_dump(mode="json") if card["latest_run"] else None}


@router.post("/cards/{card_id}/refresh", status_code=202)
def refresh(card_id: str, payload: RefreshRequest, background: BackgroundTasks,
            repository=Depends(get_repository), pipeline=Depends(get_pipeline), collector=Depends(get_collector)):
    card = checked(lambda: repository.card(card_id))
    if card["version"] != payload.version:
        raise HTTPException(409, "stale_card_version")
    if payload.leadership_url:
        host = (urlsplit(card["website"]).hostname or "").removeprefix("www.")
        source_host = (payload.leadership_url.host or "").removeprefix("www.")
        if source_host != host and not source_host.endswith("." + host):
            raise HTTPException(422, "leadership_source_must_belong_to_official_domain")
    job, created = checked(lambda: repository.new_job(card_id, payload.request_key, "collect",
        {"version": payload.version, "leadership_url": str(payload.leadership_url) if payload.leadership_url else None}))
    if created:
        background.add_task(collect_job, repository, pipeline, collector, card, job["job_id"],
                            str(payload.leadership_url) if payload.leadership_url else None)
    return job


@router.get("/jobs/{job_id}")
def get_job(job_id: str, repository=Depends(get_repository)):
    return checked(lambda: repository.job(job_id))


@router.get("/runs/{run_id}")
def get_run(run_id: str, repository=Depends(get_repository)):
    return checked(lambda: repository.get(run_id))


@router.post("/runs/{run_id}/reviews")
def review(run_id: str, payload: ReviewRequest, repository=Depends(get_repository)):
    return checked(lambda: LLMWorkflow(repository).review(run_id, ReviewBatch(
        expected_revision=payload.expected_revision, decisions=payload.decisions), actor=payload.reviewer))


@router.post("/runs/{run_id}/materials", status_code=202)
def materials(run_id: str, payload: MaterialRequest, background: BackgroundTasks,
              repository=Depends(get_repository), pipeline=Depends(get_pipeline)):
    run = checked(lambda: repository.check_current(run_id, payload.expected_revision))
    if not any(f.verification_status == "verified" for f in run.facts):
        raise HTTPException(409, "no_accepted_facts")
    job, created = checked(lambda: repository.new_job(run.institution.id, payload.request_key, "material",
                                                     {"run_id": run_id, **payload.model_dump(mode="json")}))
    if created:
        background.add_task(material_job, repository, pipeline, run_id, payload, job["job_id"])
    return job


@router.get("/materials/{material_id}/download")
def download(material_id: str, repository=Depends(get_repository)):
    _, stale = checked(lambda: repository.material(material_id))
    if stale:
        raise HTTPException(409, "material_stale_after_review")
    path = repository.path.parent / "presentations" / f"{material_id}.pptx"
    if not path.is_file():
        raise HTTPException(404, "presentation_file_not_found")
    return FileResponse(path, filename="Материалы к встрече.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@router.get("/materials")
def material_list(repository=Depends(get_repository)):
    return repository.materials()


@router.get("/materials/{material_id}/brief")
def brief(material_id: str, repository=Depends(get_repository)):
    draft, stale = checked(lambda: repository.material(material_id))
    if stale:
        raise HTTPException(409, "material_stale_after_review")
    return Response(render_markdown(draft), media_type="text/markdown; charset=utf-8")
