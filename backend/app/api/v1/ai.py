"""Demo 1 UI API backed by the real parser, LLM workflow and SQLite staging."""

import asyncio
import inspect
import logging
import os
import jwt
import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Annotated, Literal
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import FileResponse, Response
from pydantic import Field, HttpUrl, field_validator

from database.demo1_store import DemoRepository
from parser.config import SectionSpec, Settings, UniversityProfile
from parser.main import UniversityParser
from parser.public_http import public_site_session, public_url
from parser.utils import HttpClient
from logging_config import describe_error

from app.services.llm.client import ConfigurationError, GigaChatProvider, GigaChatSettings, LLMError
from app.auth.security import decode_access_token
from app.services.llm.card_chat import HISTORY_LIMIT, answer_card
from app.services.llm.card_intake import (
    continue_intake, intake_options_answer, is_short_name, plan_collection, registry_intake_options,
    short_creation_request,
)
from app.services.llm.collection_intent import named_update_query, refresh_scope, refresh_sources
from app.services.llm.general_chat import answer_general, registry_answer
from app.services.llm.models import Institution, MeetingContext, StrictModel
from app.services.llm.official_sources import official_leadership_document, official_reference_document, official_self_study_url
from app.services.llm.partnership import (
    MATCH_CATALOG, REGIONAL_CASES, PartnershipMatch, event_is_upcoming, match_partnership, partnership_facts,
)
from app.services.llm.pipeline import AssistantPipeline, DraftValidationError, fact_needs_recheck
from app.services.llm.render import render_markdown
from app.services.llm.workflow import LLMWorkflow
from app.services.llm.workflow_models import ReviewBatch, WorkflowError
from app.services.parser_service import resolve_site
from app.services.llm.changes import compare_run, effective_facts
from app.services.meeting_pptx import build_meeting_presentation
from app.services.dossier_preview import build_dossier_preview
from app.services.pptx_preview import preview_presentation

router = APIRouter(prefix="/ai", tags=["Демо 1: сбор, проверка и презентация"])
chat_bearer = HTTPBearer(auto_error=False)
LOGGER = logging.getLogger(__name__)
ACTIVE_COLLECTIONS: dict[str, dict] = {}
ACTIVE_COLLECTIONS_LOCK = threading.Lock()


def chat_user(credentials: HTTPAuthorizationCredentials | None = Depends(chat_bearer)):
    if credentials:
        try:
            return decode_access_token(credentials.credentials)["sub"]
        except (jwt.InvalidTokenError, RuntimeError, ValueError):
            pass
    raise HTTPException(401, "chat_login_required", headers={"WWW-Authenticate": "Bearer"})


@lru_cache(maxsize=1)
def get_repository():
    default = Path(__file__).resolve().parents[3] / "output/mvp/registry.sqlite3"
    sqlite_path = Path(os.environ.get("VUZ_MVP_DB", str(default)))
    return DemoRepository(sqlite_path)


@lru_cache(maxsize=1)
def get_pipeline():
    try:
        # 2000 для контекстных полей (GigaChat обрезает длинный JSON), 4000 для
        # фрагментов руководства (там крупные фрагменты и меньше фактов).
        return AssistantPipeline(GigaChatProvider(GigaChatSettings.from_env()), chunk_size=2000,
                                 overlap=200, max_chunks=32, max_fragment_size=4000,
                                 fragment_chunk_size=4000)
    except ConfigurationError:
        raise HTTPException(503, "gigachat_configuration_required") from None


def get_collector():
    def collect(website, name, leadership_url=None, *, biography_url=None, institution_urls=(), pdf_sources=(),
                include_institution=False, public_site_only=False, cancel_event=None):
        settings = Settings.from_env()
        # The LLM needs source documents from the official site, including on refresh.
        settings.max_retries = 1
        settings.timeout = min(settings.timeout, 20)
        settings.max_total_requests = min(settings.max_total_requests, 30)
        settings.fetch_item_pages = False
        # Догружаем только несколько страниц мероприятий: там часто указана
        # дата начала события, которой нет в карточке списка.
        settings.fetch_event_pages = True
        settings.max_event_documents = min(settings.max_event_documents, 3)
        settings.max_bios = min(settings.max_bios, 3)
        # SvelteKit-сайты (например, МГУ) отдают пустой HTML без браузера.
        settings.enable_playwright = True
        # A refresh must attempt the live source, not silently refresh cached dates.
        settings.use_cache = False
        # Паспорт агрегаторов и поиск новостей в документы для LLM не попадают,
        # а занимают минуты: в этом сценарии они не нужны.
        settings.use_aggregators = False
        settings.use_news_search = False
        client = HttpClient(settings, session=public_site_session()) if public_site_only else None
        parser = UniversityParser(settings, client=client)
        if cancel_event is not None:
            parser.client.cancel_event = cancel_event
        try:
            profile = (UniversityProfile(slug="selected-official-sources", name=name, url=website,
                       sections=(SectionSpec(name="leadership", url=leadership_url),) if leadership_url else (),
                       bio_urls=(biography_url,) if biography_url else ()) if leadership_url or biography_url else None)
            allowed_sections = (
                ("about", "leadership", "contacts", "programs", "research", "projects",
                 "grants", "partnerships", "news", "events")
                if include_institution
                else ("about", "leadership", "contacts") if not leadership_url
                else ("leadership",)
            )
            result = parser.parse(url=website, name=name, profile=profile,
                                  allowed_sections=allowed_sections)
            if not leadership_url:
                official = official_leadership_document(parser.client, website, result.documents,
                                                        max_chars=settings.max_document_chars)
                if official:
                    result.documents.insert(0, official)
            existing = {document.url: document for document in result.documents}
            reference_documents = []
            for url in institution_urls:
                document = official_reference_document(parser.client, url, website, name,
                                                       max_chars=settings.max_document_chars)
                if document is not None:
                    reference_documents.append(document)
                else:
                    reference_documents.extend(parser.collect_institution_documents([url]))
            if len(reference_documents) < len(institution_urls):
                result.warnings.append("Часть справочных HTML-страниц не получена. Для PDF используйте отдельное поле отчёта.")
            auto_report_url = None
            if include_institution and not leadership_url:
                known_paths = {urlsplit(url).path.rstrip("/") for url in (*existing, *institution_urls)}
                if not any(urlsplit(url).path.rstrip("/") == "/sveden/common" for url in institution_urls):
                    common = official_reference_document(parser.client, urljoin(website, "/sveden/common/"),
                                                         website, name, max_chars=settings.max_document_chars)
                    if common is not None:
                        reference_documents.append(common)
                if not pdf_sources:
                    auto_report_url = official_self_study_url(parser.client, website)
            report_sources = pdf_sources or ([{"url": auto_report_url}] if auto_report_url else [])
            pdf_documents, pdf_errors = parser.collect_pdf_documents(report_sources)
            result.errors.extend(pdf_errors)
            reference_documents.extend(pdf_documents)
            if include_institution and not leadership_url:
                for path in ("/sveden/education", "/sveden/objects"):
                    if path in known_paths:
                        continue
                    reference = official_reference_document(
                        parser.client, urljoin(website, path + "/"), website, name,
                        max_chars=settings.max_document_chars,
                    )
                    if reference is not None:
                        reference_documents.append(reference)
            for document in pdf_documents:
                if document.metadata.get("partial_pdf") == "true":
                    result.warnings.append(f"PDF: обработаны страницы {document.metadata['pdf_selected_pages']} из {document.metadata['pdf_page_count']}; полный отчёт не обработан.")
                if document.metadata.get("pdf_pages_without_text"):
                    result.warnings.append("pdf_pages_without_text_ocr_required")
            for document in reversed(reference_documents):
                previous = existing.get(document.url)
                if previous:
                    result.documents.remove(previous)
                result.documents.insert(0, document)
                existing[document.url] = document
            if leadership_url:
                # The homepage was used for discovery, not selected as a fact source.
                allowed_categories = (
                    {"organization", "leadership", "contacts", "research", "projects",
                     "grants", "project_news", "partnerships", "achievements", "other"}
                    if include_institution
                    else {"leadership", "contacts"}
                )
                result.documents = [document for document in result.documents
                                    if document.category in allowed_categories]
            if include_institution:
                from parser.institution_context import mark_institution_context
                for document in result.documents:
                    mark_institution_context(document, name)
            return result
        finally:
            parser.close()
    return collect


def schedule_collection(background, repository, pipeline, collector, card, job_id,
                        leadership_url=None, source_options=None):
    cancel_event = threading.Event()
    active = {"event": cancel_event, "task": None, "stage": "queued"}
    with ACTIVE_COLLECTIONS_LOCK:
        ACTIVE_COLLECTIONS[job_id] = active
    background.add_task(
        collect_job,
        repository,
        pipeline,
        collector,
        card,
        job_id,
        leadership_url,
        source_options,
        cancel_event=cancel_event,
    )


class CardCreate(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    city: str = Field(default="", max_length=160)
    website: HttpUrl | None = None

    @field_validator("website", mode="before")
    @classmethod
    def blank_website(cls, value):
        return value or None


class CardIntakeCreate(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    city: str = Field(default="", max_length=160)
    website: HttpUrl | None = None
    request_key: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=150)


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


class PartnershipRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    meeting: MeetingContext = Field(default_factory=MeetingContext)


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=2000)
    request_key: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=100)


class RecheckRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    fact_ids: list[str] = Field(min_length=1, max_length=1000)
    source_checked: Literal[True]
    reviewer: str = Field(min_length=1, max_length=160)


class MaterialRequest(StrictModel):
    presentation_variant: Literal["compact", "detailed"] = "detailed"
    expected_revision: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=150)
    meeting: MeetingContext = Field(default_factory=MeetingContext)
    fact_ids: list[str] | None = None
    rector_photo_url: HttpUrl | None = None
    partnership_matches: list[PartnershipMatch] = Field(default_factory=list, max_length=12)

    @field_validator("rector_photo_url")
    @classmethod
    def photo_must_use_https(cls, value):
        if value is not None and value.scheme != "https":
            raise ValueError("Portrait URLs must use HTTPS")
        return value


def checked(operation):
    try:
        return operation()
    except WorkflowError as exc:
        code = str(exc)
        raise HTTPException(404 if code.endswith("not_found") else 409, code) from None


async def checked_async(operation):
    try:
        result = operation()
        return await result if inspect.isawaitable(result) else result
    except WorkflowError as exc:
        code = str(exc)
        raise HTTPException(404 if code.endswith("not_found") else 409, code) from None


async def collect_job(repository, pipeline, collector, card, job_id, leadership_url=None,
                      source_options=None, *, cancel_event=None):
    task = asyncio.current_task()
    run_id = None
    with ACTIVE_COLLECTIONS_LOCK:
        active = ACTIVE_COLLECTIONS.get(job_id)
        if active is not None:
            active["task"] = task
            active["stage"] = "parser"
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        started = perf_counter()
        repository.update_job(job_id, status="running", progress=15, message="Сбор официальных страниц руководства")
        collector_options = dict(source_options or {})
        if cancel_event is not None:
            collector_options["cancel_event"] = cancel_event
        parsed = await asyncio.to_thread(
            collector, card["website"], card["name"], leadership_url, **collector_options
        )
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        LOGGER.info("Source collection completed: job_id=%s elapsed_seconds=%.2f documents=%d warnings=%d errors=%d",
                    job_id, perf_counter() - started, len(parsed.documents), len(parsed.warnings), len(parsed.errors))
        for parse_warning in parsed.warnings:
            LOGGER.warning("Предупреждение парсера: вуз=%s, причина=%s",
                           card["name"], describe_error(parse_warning))
        for parse_error in parsed.errors:
            LOGGER.error("Ошибка парсера: вуз=%s, причина=%s", card["name"], describe_error(parse_error))
        no_documents = not parsed.documents
        documents_note = ""
        if no_documents:
            documents_note = (parsed.errors or parsed.warnings or ["нет документов"])[0]
        repository.update_job(job_id, progress=55, message="Страницы получены. Извлечение фактов из текстов")
        workflow = LLMWorkflow(repository, pipeline)
        research, _ = workflow.create(Institution(id=card["id"], name=card["name"], card_version=card["version"]),
                                       parsed, job_id, modes=("0.2", "0.3") if (source_options or {}).get("include_institution") else ("0.2",))
        run_id = research.run_id
        repository.set_latest(card["id"], research.run_id)
        with ACTIVE_COLLECTIONS_LOCK:
            active = ACTIVE_COLLECTIONS.get(job_id)
            if active is not None:
                active["stage"] = "llm"
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        repository.update_job(job_id, progress=55, message="GigaChat обрабатывает первый запрос")

        def update_extraction_progress(mode, processed, total, _chunk_id):
            progress = min(94, 55 + int(39 * processed / max(1, total)))
            try:
                repository.update_job(
                    job_id,
                    progress=progress,
                    message=f"Извлечение через GigaChat: схема {mode}, запрос {processed} из примерно {total}",
                )
            except Exception:
                LOGGER.exception("Не удалось обновить прогресс извлечения: задание=%s", job_id)

        extraction_started = perf_counter()
        provider = pipeline.provider
        async with provider.session() if isinstance(provider, GigaChatProvider) else nullcontext():
            research = await workflow.extract(research.run_id, meeting_profile=not leadership_url, context_fields=[
                "institution.profile", "institution.type", "institution.location", "institution.founded",
                "institution.education_areas", "institution.education_levels", "institution.students",
                "institution.teaching_staff", "institution.budget_students", "institution.dormitory_capacity",
                "institution.dormitory_occupancy", "institution.structure", "project.name", "project.program",
                "project.role", "project.partner", "project.status", "project.period", "project.description",
                "event.name", "event.type", "event.date", "event.status", "event.description"],
                progress_callback=update_extraction_progress)
        LOGGER.info("Fact extraction completed: job_id=%s run_id=%s elapsed_seconds=%.2f status=%s facts=%d",
                    job_id, research.run_id, perf_counter() - extraction_started, research.status, len(research.facts))
        stage_errors = list(research.errors)
        for result in research.results.values():
            stage_errors.extend(error.code for error in result.errors)
        unique_errors = list(dict.fromkeys(stage_errors))
        for error_code in unique_errors:
            normalized_code = error_code.split(":", 1)[-1]
            LOGGER.error(
                "Ошибка извлечения фактов: вуз=%s, задание=%s, причина=%s",
                card["name"], job_id, describe_error(normalized_code),
            )
        has_candidates = any(fact.validation_status == "passed" and not fact.issues for fact in research.facts)
        if research.status == "failed" or not has_candidates:
            if no_documents:
                LOGGER.error(
                    "Сбор остановлен: вуз=%s, задание=%s, причина=%s",
                    card["name"], job_id, describe_error(documents_note),
                )
                repository.update_job(
                    job_id,
                    status="failed",
                    progress=100,
                    message=f"Парсер не получил ни одного документа: {documents_note}",
                    result={"run_id": research.run_id, "code": "no_source_documents"},
                )
                return
            LOGGER.error("Сбор не завершён: вуз=%s, задание=%s", card["name"], job_id)
            message = ("Все найденные записи отклонены проверкой. Причины доступны в карточке."
                       if research.facts else "Не удалось извлечь проверяемые факты")
            repository.update_job(job_id, status="failed", progress=100, message=message,
                                  result={"run_id": research.run_id, "code": unique_errors[0].split(":", 1)[-1] if unique_errors else "no_valid_extraction"})
        else:
            message = "Сведения собраны. Проверьте изменения"
            if research.reconfirmed_facts:
                message += f". Подтверждено повторным сбором: {len(research.reconfirmed_facts)}"
            if research.status == "partial":
                message += ". Часть источников или сведений не обработана"
                LOGGER.warning(
                    "Сбор завершён частично: вуз=%s, задание=%s, найдено фактов=%s, ошибок=%s",
                    card["name"], job_id, len(research.facts), len(unique_errors),
                )
            else:
                LOGGER.info(
                    "Всё успешно: вуз=%s, задание=%s, найдено фактов=%s",
                    card["name"], job_id, len(research.facts),
                )
            repository.update_job(job_id, status="completed", progress=100,
                                  message=message, result={"run_id": research.run_id})
    except asyncio.CancelledError:
        LOGGER.warning("Сбор отменён: вуз=%s, задание=%s", card["name"], job_id)
        repository.update_job(job_id, status="cancelled", progress=100,
                              message="Сбор остановлен по запросу пользователя",
                              result={"code": "collection_cancelled", **({"run_id": run_id} if run_id else {})})
    except (WorkflowError, LLMError) as exc:
        LOGGER.error("Сбой сбора: вуз=%s, задание=%s, причина=%s",
                     card["name"], job_id, describe_error(exc))
        repository.update_job(job_id, status="failed", progress=100,
                              message="Сбор не завершился. Проверьте сайт и настройки ИИ.",
                              result={"code": str(exc)})
    except Exception:
        LOGGER.exception("Внутренняя ошибка сбора: вуз=%s, задание=%s", card["name"], job_id)
        repository.update_job(job_id, status="failed", message="Ошибка сбора. Повторите попытку или уточните адрес сайта",
                              result={"code": "collection_failed"})
    finally:
        with ACTIVE_COLLECTIONS_LOCK:
            if ACTIVE_COLLECTIONS.get(job_id) is active:
                ACTIVE_COLLECTIONS.pop(job_id, None)


async def material_job(repository, pipeline, run_id, payload, job_id):
    try:
        repository.update_job(job_id, status="running", progress=30, message="Подготовка тезисов по принятым фактам")
        material_id, draft = await LLMWorkflow(repository, pipeline).generate(
            run_id, payload.expected_revision, fact_ids=payload.fact_ids, meeting=payload.meeting,
            presentation_variant=payload.presentation_variant)
        repository.update_job(job_id, progress=80, message="Сборка карточки PowerPoint по шаблону")
        path = repository.path.parent / "presentations" / f"{material_id}.pptx"
        await asyncio.to_thread(build_meeting_presentation, draft, path, repository.material_facts(material_id),
                                rector_photo_url=str(payload.rector_photo_url) if payload.rector_photo_url else None,
                                partnership_matches=[item.model_dump(mode="json") for item in payload.partnership_matches])
        # A review or refresh during file assembly invalidates this export too.
        _, stale = repository.material(material_id)
        if stale:
            raise WorkflowError("facts_changed_during_generation")
        repository.save_material_to_reports(material_id, "PPTX")
        repository.update_job(job_id, status="completed", progress=100, message="Презентация сформирована",
            result={"material_id": material_id, "download_url": f"/api/v1/ai/materials/{material_id}/download",
                    "brief_url": f"/api/v1/ai/materials/{material_id}/brief",
                    "presentation_variant": payload.presentation_variant})
        LOGGER.info("Всё успешно: презентация сформирована, задание=%s", job_id)
    except asyncio.CancelledError:
        LOGGER.warning("Формирование материалов отменено: задание=%s", job_id)
        repository.update_job(job_id, status="failed", message="Формирование прервано", result={"code": "generation_cancelled"})
        raise
    except (WorkflowError, DraftValidationError, LLMError) as exc:
        message = ("Истёк срок проверки фактов. Повторно сверьте их перед экспортом."
                   if str(exc) == "facts_require_freshness_check" else "Материалы не сформированы")
        LOGGER.error("Не удалось сформировать материалы: задание=%s, причина=%s", job_id, describe_error(exc))
        repository.update_job(job_id, status="failed", message=message, result={"code": str(exc)})
    except Exception:
        LOGGER.exception("Внутренняя ошибка формирования материалов: задание=%s", job_id)
        repository.update_job(job_id, status="failed", message="Ошибка формирования презентации",
                              result={"code": "material_generation_failed"})


@router.get("/cards")
async def list_cards(repository=Depends(get_repository)):
    return await checked_async(lambda: repository.cards())


@router.post("/cards", status_code=201)
async def create_card(payload: CardCreate, repository=Depends(get_repository)):
    # Ссылку можно не передавать: официальный сайт находится по названию.
    website = await resolve_intake_website(
        payload.name, str(payload.website) if payload.website else None
    )
    return await checked_async(
        lambda: repository.create_card(payload.name, payload.city, website)
    )


@router.delete("/cards/{card_id}", status_code=204, summary="Полностью удалить карточку и связанные данные")
async def delete_card(card_id: str, user_id=Depends(chat_user), repository=Depends(get_repository)):
    """Purge a card and its workflow data; requires an authenticated user."""
    await checked_async(lambda: repository.delete_card(card_id, actor=user_id))
    return Response(status_code=204)
async def resolve_intake_website(name: str, website: str | None) -> str:
    if website:
        candidate = website
    else:
        try:
            candidate = await asyncio.to_thread(resolve_site, name)
        except Exception:
            LOGGER.exception("Не удалось найти официальный сайт учреждения: %s", name)
            raise HTTPException(502, "official_site_discovery_failed") from None
    if not candidate:
        raise HTTPException(422, "official_site_not_found")
    try:
        candidate = str(HttpUrl(candidate)).rstrip("/")
        public_url(candidate)
    except ValueError:
        raise HTTPException(422, "public_website_required") from None
    return candidate


async def enqueue_card_intake(name, city, website, request_key, background, repository, pipeline, collector):
    source_options = {"include_institution": True, "public_site_only": True}
    card, job, created = await checked_async(lambda: repository.start_card_intake(
        name, city, website, request_key, source_options,
    ))
    if created:
        schedule_collection(background, repository, pipeline, collector, card, job["job_id"],
                            source_options=source_options)
    return {"card": card, "job": job}


@router.post("/cards/intake", status_code=202)
async def create_card_intake(payload: CardIntakeCreate, background: BackgroundTasks,
                             repository=Depends(get_repository), pipeline=Depends(get_pipeline),
                             collector=Depends(get_collector)):
    website = await resolve_intake_website(payload.name, str(payload.website) if payload.website else None)
    return await enqueue_card_intake(payload.name, payload.city, website, f"ui:{payload.request_key}",
                                     background, repository, pipeline, collector)


@router.get("/cards/{card_id}")
async def get_card(card_id: str, repository=Depends(get_repository)):
    card = await checked_async(lambda: repository.card(card_id))
    run = repository.get(card["latest_run"]) if card["latest_run"] else None
    return {**card, "job": repository.card_job(card_id),
            "run": run.model_dump(mode="json") if run else None,
            "approved_facts": effective_facts(run) if run else []}


@router.get("/cards/{card_id}/history")
async def history(card_id: str, repository=Depends(get_repository)):
    await checked_async(lambda: repository.card(card_id))
    return [{"run_id": run.run_id, "revision": run.revision, "status": run.status,
             "created_at": run.created_at, "previous_run_id": run.previous_run_id,
             "approved_count": len(effective_facts(run)), "candidate_count": len(run.facts)}
            for run in repository.history(card_id)]


@router.get("/cards/{card_id}/chat")
def chat_history(card_id: str, user_id=Depends(chat_user), repository=Depends(get_repository)):
    _, _, turns = checked(lambda: repository.chat_snapshot(card_id, user_id, HISTORY_LIMIT))
    return [{"id": turn["id"], "question": turn["question"], "answer": turn["answer"]} for turn in turns]


async def intake_confirmation(repository, answer):
    args = answer.get("collection_request")
    if args and not args.get("website") and (is_short_name(args["name"]) or answer.get("requires_confirmation")):
        name = args["name"]
        choices = registry_intake_options(name, await checked_async(repository.cards))
        if not choices:
            try:
                website = await resolve_intake_website(" ".join(filter(None, (name, args["city"]))), None)
            except HTTPException as exc:
                if exc.detail not in {"official_site_not_found", "official_site_discovery_failed", "public_website_required"}:
                    raise
            else:
                choices = [{**args, "website": website}]
        return {**intake_options_answer(name, choices), "intake_goal": answer.get("intake_goal")}
    return answer


async def start_chat_collection(args, payload, user_id, background, repository, pipeline, collector, *, confirmation=None):
    website = await resolve_intake_website(args["name"], args.get("website"))
    key = f"confirmed:{confirmation}" if confirmation else payload.request_key
    return await enqueue_card_intake(args["name"], args["city"], website,
                                     f"chat:{user_id}:{key}",
                                     background, repository, pipeline, collector)


@router.get("/chat/general")
def general_chat_history(user_id=Depends(chat_user), repository=Depends(get_repository)):
    return repository.general_chat_history(user_id, HISTORY_LIMIT)


@router.post("/chat/general")
async def general_chat_message(payload: ChatRequest, background: BackgroundTasks, user_id=Depends(chat_user),
                               repository=Depends(get_repository), pipeline=Depends(get_pipeline),
                               collector=Depends(get_collector)):
    turns = repository.general_chat_history(user_id, HISTORY_LIMIT)
    try:
        answer = (short_creation_request(payload.message) or await continue_intake(pipeline.provider, payload.message, turns)
                  or await answer_general(pipeline.provider, turns, payload.message))
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from None
    except WorkflowError as exc:
        raise HTTPException(422, str(exc)) from None
    if "registry_query" in answer:
        answer = registry_answer(await checked_async(lambda: repository.cards()), answer["registry_query"])
        if answer.get("pending_institution"):
            answer["intake_goal"] = payload.message
    answer = await intake_confirmation(repository, answer)
    starts_collection = "collection_request" in answer
    if starts_collection:
        turn_id = answer.get("confirmed_turn_id")
        collection = await start_chat_collection(
            answer["collection_request"], payload, user_id, background, repository, pipeline, collector,
            confirmation=f"general:{turn_id}" if turn_id is not None else None,
        )
        job = collection["job"]
        goal = answer.get("intake_goal")
        answer = {
            "text": job["message"] + ". Ход сбора и результат видны в карточке.",
            "sources": [], "cards": [collection["card"]],
        }
        if goal:
            answer["intake_goal"] = goal
            answer["text"] += " После сбора сверьте сведения, чтобы продолжить подготовку материалов."
    return checked(lambda: repository.save_general_chat_turn(
        user_id, turns[-1]["id"] if turns else None, payload.message, answer, require_current=not starts_collection,
    ))


@router.post("/chat/intake")
async def chat_intake(payload: ChatRequest, background: BackgroundTasks, user_id=Depends(chat_user),
                      repository=Depends(get_repository), pipeline=Depends(get_pipeline),
                      collector=Depends(get_collector)):
    try:
        explicit = short_creation_request(payload.message)
        args = explicit["collection_request"] if explicit else await plan_collection(pipeline.provider, payload.message)
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from None
    except WorkflowError as exc:
        raise HTTPException(422, str(exc)) from None
    if args is None:
        return {"message": "Укажите название учреждения и попросите создать карточку. Официальный сайт можно не указывать."}
    answer = await intake_confirmation(repository, {"collection_request": args})
    if "collection_request" not in answer:
        return {"message": answer["text"] + "\nДля подтверждения откройте чат агента."}
    return {"collection": await start_chat_collection(args, payload, user_id, background, repository, pipeline, collector)}


@router.post("/cards/{card_id}/chat")
async def chat_message(card_id: str, payload: ChatRequest, background: BackgroundTasks, user_id=Depends(chat_user),
                       repository=Depends(get_repository), pipeline=Depends(get_pipeline),
                       collector=Depends(get_collector)):
    card, run, turns = checked(lambda: repository.chat_snapshot(card_id, user_id, HISTORY_LIMIT))
    run_id, revision = (run.run_id, run.revision) if run else (None, None)
    context = [turn for turn in turns if (turn["run_id"], turn["revision"]) == (run_id, revision)]
    registry = await checked_async(repository.cards) if named_update_query(payload.message) else None
    direction = refresh_scope(payload.message, card, registry) if registry is not None else None
    try:
        answer = (short_creation_request(payload.message) or await continue_intake(pipeline.provider, payload.message, turns)
                  or direction or await answer_card(pipeline.provider, card, run, context, payload.message))
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from None
    except WorkflowError as exc:
        raise HTTPException(422, str(exc)) from None
    if "registry_query" in answer:
        answer = registry_answer(await checked_async(repository.cards), answer["registry_query"])
        if answer.get("pending_institution"):
            answer["intake_goal"] = payload.message
    answer = await intake_confirmation(repository, answer)
    last_id = turns[-1]["id"] if turns else None
    if "collection_request" in answer:
        turn_id = answer.get("confirmed_turn_id")
        if turn_id is not None:
            acknowledgement = {"text": "Запускаю сбор по подтверждённому учреждению.", "sources": [],
                               "intake_goal": answer.get("intake_goal")}
            checked(lambda: repository.save_chat_turn(
                card_id, user_id, run_id, revision, last_id, payload.message, acknowledgement,
            ))
        return {"collection": await start_chat_collection(
            answer["collection_request"], payload, user_id, background, repository, pipeline, collector,
            confirmation=f"{card_id}:{turn_id}" if turn_id is not None else None,
        )}
    if "refresh_request" in answer:
        if registry is None:
            registry = await checked_async(repository.cards)
        scope = refresh_scope(payload.message, card, registry)
        if scope:
            return checked(lambda: repository.save_chat_turn(
                card_id, user_id, run_id, revision, last_id, payload.message, scope,
            ))
        try:
            public_url(card["website"])
        except ValueError:
            answer = {"text": "Для сбора нужен официальный сайт вуза. Укажите публичный адрес в карточке.",
                      "sources": []}
        else:
            source_options = {"include_institution": True, "public_site_only": True}
            try:
                urls = refresh_sources(payload.message, card["website"])
            except ValueError:
                answer = {"text": "Для сбора укажите до трёх ссылок на публичные страницы официального сайта открытого вуза.",
                          "sources": []}
                return checked(lambda: repository.save_chat_turn(
                    card_id, user_id, run_id, revision, last_id, payload.message, answer,
                ))
            if urls:
                source_options["institution_urls"] = urls
            identity = {"version": card["version"], **source_options}
            key = f"chat-refresh:{user_id}:{payload.request_key}"
            turn, job, created = checked(lambda: repository.save_chat_turn(
                card_id, user_id, run_id, revision, last_id, payload.message, answer,
                refresh=(key, identity),
            ))
            if created:
                schedule_collection(background, repository, pipeline, collector, card, job["job_id"],
                                    source_options=source_options)
            return turn
    return checked(lambda: repository.save_chat_turn(
        card_id, user_id, run_id, revision, last_id, payload.message, answer,
    ))


@router.post("/cards/{card_id}/chat/{turn_id}/confirm")
async def confirm_chat_edit(card_id: str, turn_id: int, user_id=Depends(chat_user),
                            repository=Depends(get_repository)):
    checked(lambda: repository.confirm_chat_edit(card_id, user_id, turn_id))
    return {"status": "applied"}


@router.post("/cards/{card_id}/refresh", status_code=202)
async def refresh(card_id: str, payload: RefreshRequest, background: BackgroundTasks,
            repository=Depends(get_repository), pipeline=Depends(get_pipeline), collector=Depends(get_collector)):
    card = await checked_async(lambda: repository.card(card_id))
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
        schedule_collection(background, repository, pipeline, collector, card, job["job_id"],
                            str(payload.leadership_url) if payload.leadership_url else None, source_options)
    return job


@router.get("/jobs/{job_id}")
def get_job(job_id: str, repository=Depends(get_repository)):
    return checked(lambda: repository.job(job_id))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, repository=Depends(get_repository)):
    job = checked(lambda: repository.job(job_id))
    if job.get("task_type") != "collect":
        raise HTTPException(409, "job_not_cancellable")
    if job.get("status") not in {"queued", "running", "cancelling"}:
        return job

    with ACTIVE_COLLECTIONS_LOCK:
        active = ACTIVE_COLLECTIONS.get(job_id)
        if active is not None:
            active["event"].set()
            task = active.get("task")
            stage = active.get("stage")
        else:
            task = None
            stage = None

    if active is None:
        current = checked(lambda: repository.job(job_id))
        if current.get("status") not in {"queued", "running", "cancelling"}:
            return current
        repository.update_job(
            job_id, status="cancelled", progress=100,
            message="Сбор остановлен по запросу пользователя",
            result={"code": "collection_cancelled"},
        )
    else:
        repository.update_job(job_id, status="cancelling", message="Останавливаю сбор данных")
        # During parsing the worker thread stops cooperatively at the next request
        # boundary. During GigaChat extraction, cancel the async request directly.
        if task is not None and stage == "llm":
            task.cancel()
    LOGGER.info("Пользователь остановил сбор: задание=%s", job_id)
    return checked(lambda: repository.job(job_id))


@router.get("/runs/{run_id}")
def get_run(run_id: str, repository=Depends(get_repository)):
    return checked(lambda: repository.get(run_id))


@router.get("/runs/{run_id}/changes")
def changes(run_id: str, repository=Depends(get_repository)):
    comparison = compare_run(checked(lambda: repository.get(run_id)))
    now = datetime.now(timezone.utc)
    comparison["recheck_fact_ids"] = [fact.fact_id for fact in comparison["approved_facts"]
                                     if fact_needs_recheck(fact, now)]
    return comparison


@router.post("/runs/{run_id}/recheck")
def recheck(run_id: str, payload: RecheckRequest, repository=Depends(get_repository)):
    return checked(lambda: LLMWorkflow(repository).recheck(
        run_id, payload.expected_revision, payload.fact_ids, actor=payload.reviewer,
    ))


@router.get("/runs/{run_id}/preview")
async def preview(run_id: str, expected_revision: int, presentation_variant: Literal["compact", "detailed"] = "detailed",
                  repository=Depends(get_repository)):
    run = checked(lambda: repository.check_current(run_id, expected_revision))
    excluded = set(run.retired_fact_ids) | run.reconfirmed_candidate_ids
    facts = [f for f in run.facts if f.fact_id not in excluded and f.validation_status == "passed"
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
async def review(run_id: str, payload: ReviewRequest, repository=Depends(get_repository)):
    result = checked(lambda: LLMWorkflow(repository).review(run_id, ReviewBatch(
        expected_revision=payload.expected_revision, decisions=payload.decisions,
        retirements=payload.retirements), actor=payload.reviewer))
    return result


@router.post("/runs/{run_id}/partnership-matches")
async def recommend_partnership(
    run_id: str, payload: PartnershipRequest, repository=Depends(get_repository),
    pipeline=Depends(get_pipeline),
) -> list[PartnershipMatch]:
    run = checked(lambda: repository.check_current(run_id, payload.expected_revision))
    facts = partnership_facts(run)
    if not facts:
        return []
    try:
        matches = await match_partnership(pipeline.provider, facts, payload.meeting)
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from None
    checked(lambda: repository.check_current(run_id, payload.expected_revision))
    return matches


@router.post("/runs/{run_id}/materials", status_code=202)
def materials(run_id: str, payload: MaterialRequest, background: BackgroundTasks,
              repository=Depends(get_repository), pipeline=Depends(get_pipeline)):
    try:
        run, selected = checked(lambda: LLMWorkflow(repository).material_facts(
            run_id, payload.expected_revision, payload.fact_ids,
        ))
    except DraftValidationError as exc:
        raise HTTPException(422, str(exc)) from None
    accepted_ids = {fact.fact_id for fact in selected}
    now = datetime.now(timezone.utc)
    for match in payload.partnership_matches:
        if not set(match.basis_fact_ids) <= accepted_ids:
            raise HTTPException(422, "partnership_match_requires_selected_accepted_facts")
        entry = next((item for item in MATCH_CATALOG.values() if item.url == match.catalog_url), None)
        if match.regional_case and (not entry or not any(
            case == match.regional_case and product_id == entry.id
            for product_id, case in REGIONAL_CASES.values()
        )):
            raise HTTPException(422, "invalid_partnership_response")
        if match.event_start or (entry and entry.event_start):
            if (not entry or not entry.event_start or entry.event_start != match.event_start
                    or not event_is_upcoming(entry.event_start, payload.meeting, now.date())):
                raise HTTPException(422, "invalid_partnership_response")
    if any(fact_needs_recheck(fact, now, pipeline.review_window_days) for fact in selected):
        raise HTTPException(409, "facts_require_freshness_check")
    job, created = checked(lambda: repository.new_job(run.institution.id, payload.request_key, "material",
                                                     {"run_id": run_id, **payload.model_dump(mode="json")}))
    if created:
        background.add_task(material_job, repository, pipeline, run_id, payload, job["job_id"])
    return job


@router.get("/materials/{material_id}/download")
def download(material_id: str, format: Literal["pptx", "docx"] = "pptx",
             repository=Depends(get_repository)):
    draft, _ = checked(lambda: repository.material(material_id))
    directory = repository.path.parent / "presentations"
    path = directory / f"{material_id}.{format}"
    if format == "docx" and not path.is_file():
        from app.services.llm.docx_export import build_word_document
        directory.mkdir(parents=True, exist_ok=True)
        build_word_document(draft, path)
    if not path.is_file():
        raise HTTPException(404, "material_file_not_found")
    repository.save_material_to_reports(material_id, format.upper())
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


@router.put("/materials/{material_id}/report")
def save_material_to_reports(material_id: str, format: Literal["pptx", "docx"] = "pptx",
                             repository=Depends(get_repository)):
    draft, _ = checked(lambda: repository.material(material_id))
    directory = repository.path.parent / "presentations"
    path = directory / f"{material_id}.{format}"
    if format == "docx" and not path.is_file():
        from app.services.llm.docx_export import build_word_document
        directory.mkdir(parents=True, exist_ok=True)
        build_word_document(draft, path)
    if not path.is_file():
        raise HTTPException(404, "material_file_not_found")
    return checked(lambda: repository.save_material_to_reports(material_id, format.upper()))


@router.delete("/materials/{material_id}/report")
def remove_material_from_reports(material_id: str, repository=Depends(get_repository)):
    return checked(lambda: repository.remove_material_from_reports(material_id))


@router.get("/materials/{material_id}/preview")
def material_preview(material_id: str, repository=Depends(get_repository)):
    checked(lambda: repository.material(material_id))
    path = repository.path.parent / "presentations" / f"{material_id}.pptx"
    if not path.is_file():
        raise HTTPException(404, "material_file_not_found")
    return preview_presentation(path)


@router.get("/materials/{material_id}/brief")
def brief(material_id: str, repository=Depends(get_repository)):
    draft, _ = checked(lambda: repository.material(material_id))
    return Response(render_markdown(draft, repository.material_facts(material_id)), media_type="text/markdown; charset=utf-8")
