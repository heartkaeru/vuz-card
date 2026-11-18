import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status

from app.schemas.job import JobResponse, JobStatusEnum
from app.schemas.university import (
    UniversityBase,
    UniversityCreateRequest,
    UniversityDetail,
    UniversityResolveResponse,
    UniversityUpdate,
)
from app.data.mock_universities import MOCK_UNIVERSITIES_DB
from app.services.job_queue import JOBS_STORE, run_create_pipeline
from app.services.parser_service import resolve_site

router = APIRouter(prefix="/universities", tags=["Карточки ВУЗов"])


@router.post(
    "/resolve",
    response_model=UniversityResolveResponse,
    summary="Найти официальный сайт по названию вуза",
)
def resolve_university_site(payload: UniversityCreateRequest):
    """Ищет официальный сайт автоматически — ссылку вводить не нужно."""

    website = payload.website or resolve_site(payload.name)
    if not website:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Не удалось автоматически определить официальный сайт. "
                "Уточните название или передайте website."
            ),
        )
    return UniversityResolveResponse(
        name=payload.name,
        website=website,
        city=payload.city,
    )


@router.post(
    "",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Создать карточку вуза по названию (без ссылки)",
)
async def create_university(
    payload: UniversityCreateRequest,
    background_tasks: BackgroundTasks,
):
    """Создаёт черновик карточки и запускает парсер в фоне.

    Пользователь вводит только название (и, при желании, город).
    Официальный сайт, ректорат, фото ректора и контакты парсер находит сам.
    """

    website = payload.website or resolve_site(payload.name)
    if not website:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Не удалось найти официальный сайт по названию вуза",
        )

    university_id = max(MOCK_UNIVERSITIES_DB, default=0) + 1
    now = datetime.now().isoformat()
    MOCK_UNIVERSITIES_DB[university_id] = {
        "id": university_id,
        "name": payload.name,
        "short_name": payload.name[:80],
        "inn": "",
        "website": website,
        "region": "",
        "city": payload.city or "",
        "rector_fio": None,
        "version": 1,
        "updated_at": now,
        "fot_volume": None,
        "contracts_count": 0,
        "active_projects": [],
        "officials": [],
    }

    job_id = f"job_{uuid.uuid4().hex[:8]}"
    JOBS_STORE[job_id] = {
        "job_id": job_id,
        "university_id": university_id,
        "task_type": "create_card",
        "status": JobStatusEnum.IN_PROGRESS,
        "progress": 5,
        "message": "Карточка создана. Парсер ищет сайт и ректора...",
        "created_at": now,
        "completed_at": None,
        "result": {"website": website},
    }
    background_tasks.add_task(
        run_create_pipeline,
        job_id,
        university_id,
        payload.name,
        payload.city or "",
        website,
    )
    return JOBS_STORE[job_id]


@router.get("", response_model=List[UniversityBase], summary="Получить реестр ВУЗов")
def get_universities(
    search: Optional[str] = Query(None, description="Поиск по названию или краткому имени"),
    region: Optional[str] = Query(None, description="Фильтр по региону")
):
    """
    Возвращает список всех университетов для экрана реестра (с поддержкой поиска и фильтрации).
    """
    results = list(MOCK_UNIVERSITIES_DB.values())
    
    if search:
        search_lower = search.lower()
        results = [
            u for u in results 
            if search_lower in u["name"].lower() or search_lower in u["short_name"].lower()
        ]
        
    if region:
        region_lower = region.lower()
        results = [u for u in results if region_lower in u["region"].lower()]
        
    return results


@router.get("/{university_id}", response_model=UniversityDetail, summary="Получить детальную карточку ВУЗа")
def get_university_detail(university_id: int):
    """
    Возвращает полную карточку университета со списком руководства,
    контактами, статусами проверки («было / стало») и ключевыми проектами.
    """
    if university_id not in MOCK_UNIVERSITIES_DB:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ВУЗ с id={university_id} не найден"
        )
    return MOCK_UNIVERSITIES_DB[university_id]


@router.patch("/{university_id}", response_model=UniversityDetail, summary="Обновить данные карточки вручную")
def update_university(university_id: int, payload: UniversityUpdate):
    """
    Позволяет сотруднику вручную скорректировать карточку ВУЗа.
    Реализует версионирование (защиту от параллельной перезаписи данных).
    """
    if university_id not in MOCK_UNIVERSITIES_DB:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ВУЗ с id={university_id} не найден"
        )
        
    current_data = MOCK_UNIVERSITIES_DB[university_id]
    
    # Проверка версии карточки (оптимистичная блокировка)
    if payload.version != current_data["version"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Карточка была изменена другим пользователем или фоновым процессом. Обновите страницу."
        )
        
    # Применение изменений
    update_dict = payload.model_dump(exclude_unset=True, exclude={"version"})
    for key, value in update_dict.items():
        if value is not None:
            current_data[key] = value
            
    # Увеличиваем версию и время
    current_data["version"] += 1
    current_data["updated_at"] = "2026-10-27T17:00:00"
    
    return current_data
