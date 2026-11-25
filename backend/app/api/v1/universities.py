"""Supabase-backed CRUD API for universities, officials, and metrics."""

import asyncio
import logging
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.schemas.university import (
    MetricCreate,
    MetricUpdate,
    Official,
    OfficialCreate,
    OfficialUpdate,
    UniversityBase,
    UniversityCreateRequest,
    UniversityDetail,
    UniversityMetric,
    UniversityResolveResponse,
    UniversityUpdate,
)
from app.services.parser_service import resolve_site
from database.postgres.api_repository import (
    RepositoryConflict,
    RepositoryNotFound,
    SupabaseUniversityRepository,
)

LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/universities", tags=["Карточки ВУЗов"])


def get_repository():
    try:
        return SupabaseUniversityRepository()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="supabase_configuration_required") from None


def _actor(request: Request):
    claims = getattr(request.state, "auth_claims", {})
    subject = str(claims.get("sub", "authenticated-user"))
    return {"actor_type": "human", "actor_label": f"api-user:{subject}"}


async def _run(operation):
    try:
        return await operation
    except RepositoryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except RepositoryConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except HTTPException:
        raise
    except Exception as exc:
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if sqlstate == "23505":
            raise HTTPException(status_code=409, detail="university_record_conflict") from None
        if sqlstate in {"23503", "23514", "22P02"}:
            raise HTTPException(status_code=422, detail="invalid_university_data") from None
        LOGGER.error("Supabase university API operation failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="supabase_operation_failed") from None


@router.post(
    "/resolve",
    response_model=UniversityResolveResponse,
    summary="Найти официальный сайт по названию вуза",
)
async def resolve_university_site(payload: UniversityCreateRequest):
    """Ищет официальный сайт автоматически — ссылку вводить не нужно."""
    try:
        website = payload.website or await asyncio.to_thread(resolve_site, payload.name)
    except Exception:
        LOGGER.exception("Official-site discovery failed for %s", payload.name)
        raise HTTPException(status_code=502, detail="official_site_discovery_failed") from None
    if not website:
        raise HTTPException(status_code=422, detail="official_site_not_found")
    return UniversityResolveResponse(name=payload.name, website=website, city=payload.city)


@router.post("", response_model=UniversityDetail, status_code=status.HTTP_201_CREATED,
             summary="Создать карточку в Supabase")
async def create_university(payload: UniversityCreateRequest, request: Request,
                            repository: SupabaseUniversityRepository = Depends(get_repository)):
    try:
        website = payload.website or await asyncio.to_thread(resolve_site, payload.name)
    except Exception:
        LOGGER.exception("Official-site discovery failed for %s", payload.name)
        raise HTTPException(status_code=502, detail="official_site_discovery_failed") from None
    if not website:
        raise HTTPException(status_code=422, detail="official_site_not_found")
    values = {
        "name": payload.name.strip(),
        "short_name": payload.short_name.strip() if payload.short_name else payload.name.strip(),
        "region": payload.region,
        "city": payload.city,
        "website": str(website).rstrip("/"),
        "inn": payload.inn,
        "profile": payload.profile,
        "institution_type": payload.institution_type,
        "verification_status": "needs_review",
    }
    item = await _run(repository.create_university(values, **_actor(request)))
    return await _run(repository.get_university(item["id"]))


@router.get("", response_model=List[UniversityBase], summary="Получить реестр вузов из Supabase")
async def get_universities(search: str | None = Query(None), region: str | None = Query(None),
                           repository: SupabaseUniversityRepository = Depends(get_repository)):
    return await _run(repository.list_universities(search=search, region=region))


@router.get("/{university_id}", response_model=UniversityDetail, summary="Получить карточку вуза")
async def get_university_detail(university_id: UUID,
                                repository: SupabaseUniversityRepository = Depends(get_repository)):
    item = await _run(repository.get_university(str(university_id)))
    if item is None:
        raise HTTPException(status_code=404, detail="university_not_found")
    return item


@router.patch("/{university_id}", response_model=UniversityDetail, summary="Обновить карточку вручную")
async def update_university(university_id: UUID, payload: UniversityUpdate, request: Request,
                            repository: SupabaseUniversityRepository = Depends(get_repository)):
    values = payload.model_dump(exclude_unset=True, exclude={"version"})
    if values.get("website") is not None:
        values["website"] = str(values["website"])
    await _run(repository.update_university(str(university_id), values, payload.version, **_actor(request)))
    return await _run(repository.get_university(str(university_id)))


@router.delete("/{university_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Удалить карточку и связанные данные")
async def delete_university(university_id: UUID, request: Request,
                            repository: SupabaseUniversityRepository = Depends(get_repository)):
    from app.api.v1.ai import get_repository as get_ai_repository
    from app.services.llm.workflow_models import WorkflowError

    try:
        await get_ai_repository().delete_card(str(university_id), actor=_actor(request)["actor_label"])
    except WorkflowError as exc:
        code = str(exc)
        raise HTTPException(status_code=409 if code in {"university_has_children", "card_job_already_running"} else 404,
                            detail=code) from None
    except Exception as exc:
        LOGGER.error("Supabase university delete failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="supabase_operation_failed") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{university_id}/officials", response_model=list[Official], summary="Список руководства")
async def list_officials(university_id: UUID,
                         repository: SupabaseUniversityRepository = Depends(get_repository)):
    return await _run(repository.list_officials(str(university_id)))


@router.post("/{university_id}/officials", response_model=Official, status_code=status.HTTP_201_CREATED,
             summary="Добавить руководителя")
async def create_official(university_id: UUID, payload: OfficialCreate, request: Request,
                          repository: SupabaseUniversityRepository = Depends(get_repository)):
    return await _run(repository.create_official(
        str(university_id), payload.model_dump(mode="python"), **_actor(request)))


@router.get("/{university_id}/officials/{official_id}", response_model=Official,
            summary="Получить руководителя")
async def get_official(university_id: UUID, official_id: UUID,
                       repository: SupabaseUniversityRepository = Depends(get_repository)):
    officials = await _run(repository.list_officials(str(university_id)))
    item = next((official for official in officials if official["id"] == str(official_id)), None)
    if item is None:
        raise HTTPException(status_code=404, detail="official_not_found")
    return item


@router.patch("/{university_id}/officials/{official_id}", response_model=Official,
              summary="Обновить руководителя")
async def update_official(university_id: UUID, official_id: UUID, payload: OfficialUpdate, request: Request,
                          repository: SupabaseUniversityRepository = Depends(get_repository)):
    values = payload.model_dump(exclude_unset=True, mode="python")
    item = await _run(repository.update_official(str(university_id), str(official_id), values, **_actor(request)))
    return item


@router.delete("/{university_id}/officials/{official_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Удалить руководителя")
async def delete_official(university_id: UUID, official_id: UUID, request: Request,
                          repository: SupabaseUniversityRepository = Depends(get_repository)):
    await _run(repository.delete_official(str(university_id), str(official_id), **_actor(request)))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{university_id}/metrics", response_model=list[UniversityMetric], summary="Список показателей")
async def list_metrics(university_id: UUID,
                       repository: SupabaseUniversityRepository = Depends(get_repository)):
    return await _run(repository.list_metrics(str(university_id)))


@router.post("/{university_id}/metrics", response_model=UniversityMetric, status_code=status.HTTP_201_CREATED,
             summary="Добавить показатель")
async def create_metric(university_id: UUID, payload: MetricCreate, request: Request,
                        repository: SupabaseUniversityRepository = Depends(get_repository)):
    return await _run(repository.create_metric(
        str(university_id), payload.model_dump(mode="python"), **_actor(request)))


@router.get("/{university_id}/metrics/{metric_id}", response_model=UniversityMetric,
            summary="Получить показатель")
async def get_metric(university_id: UUID, metric_id: UUID,
                     repository: SupabaseUniversityRepository = Depends(get_repository)):
    metrics = await _run(repository.list_metrics(str(university_id)))
    item = next((metric for metric in metrics if metric["id"] == str(metric_id)), None)
    if item is None:
        raise HTTPException(status_code=404, detail="metric_not_found")
    return item


@router.patch("/{university_id}/metrics/{metric_id}", response_model=UniversityMetric,
              summary="Обновить показатель")
async def update_metric(university_id: UUID, metric_id: UUID, payload: MetricUpdate, request: Request,
                        repository: SupabaseUniversityRepository = Depends(get_repository)):
    values = payload.model_dump(exclude_unset=True, mode="python")
    metrics = await _run(repository.update_metric(
        str(university_id), str(metric_id), values, **_actor(request)))
    return next(metric for metric in metrics if metric["id"] == str(metric_id))


@router.delete("/{university_id}/metrics/{metric_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Удалить показатель")
async def delete_metric(university_id: UUID, metric_id: UUID, request: Request,
                        repository: SupabaseUniversityRepository = Depends(get_repository)):
    await _run(repository.delete_metric(str(university_id), str(metric_id), **_actor(request)))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
