from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.university import UniversityBase, UniversityDetail, UniversityUpdate
from app.data.mock_universities import MOCK_UNIVERSITIES_DB

router = APIRouter(prefix="/universities", tags=["Карточки ВУЗов"])


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
