import uuid
from datetime import datetime
from fastapi import APIRouter, HTTPException, BackgroundTasks, status

from app.schemas.job import JobResponse, JobStatusEnum
from app.data.mock_universities import MOCK_UNIVERSITIES_DB
from app.services.job_queue import JOBS_STORE, run_refresh_pipeline

router = APIRouter(tags=["Фоновые задачи и актуализация данных"])


@router.post(
    "/universities/{university_id}/refresh", 
    response_model=JobResponse, 
    status_code=status.HTTP_202_ACCEPTED,
    summary="Запустить ИИ-актуализацию данных ВУЗа"
)
async def trigger_university_refresh(university_id: int, background_tasks: BackgroundTasks):
    """
    Запускает асинхронную процедуру сбора и анализа данных:
    1. Парсер обходит разделы сайта ВУЗа (ректорат, контакты).
    2. Нейросеть извлекает актуальный состав руководства.
    3. Сервер формирует предложение изменений («было / стало») со статусом ai_draft.
    
    Возвращает 202 Accepted и job_id для отслеживания прогресса.
    """
    if university_id not in MOCK_UNIVERSITIES_DB:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ВУЗ с id={university_id} не найден"
        )
        
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    created_at = datetime.now().isoformat()
    
    # Создаем запись задачи в хранилище
    JOBS_STORE[job_id] = {
        "job_id": job_id,
        "university_id": university_id,
        "task_type": "refresh_officials",
        "status": JobStatusEnum.IN_PROGRESS,
        "progress": 5,
        "message": "Инициализация фоновой задачи сбора данных...",
        "created_at": created_at,
        "completed_at": None,
        "result": None
    }
    
    # Отправляем в асинхронную очередь FastAPI
    background_tasks.add_task(run_refresh_pipeline, job_id, university_id)
    
    return JOBS_STORE[job_id]


@router.get("/jobs/{job_id}", response_model=JobResponse, summary="Проверить статус фоновой задачи")
def get_job_status(job_id: str):
    """
    Опрос статуса фоновой задачи клиентом (Polling).
    Фронтенд вызывает этот эндпоинт каждые 1-2 секунды для обновления прогресс-бара.
    """
    if job_id not in JOBS_STORE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Задача с id={job_id} не найдена"
        )
    return JOBS_STORE[job_id]
