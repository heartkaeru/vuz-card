"""Compatibility routes for the Supabase-backed AI refresh workflow."""

from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.schemas.job import JobResponse, JobStatusEnum

router = APIRouter(tags=["Фоновые задачи и актуализация данных"])


def _job_response(job: dict, university_id: str | None = None):
    status_map = {
        "queued": JobStatusEnum.PENDING,
        "running": JobStatusEnum.IN_PROGRESS,
        "completed": JobStatusEnum.COMPLETED,
        "failed": JobStatusEnum.FAILED,
    }
    return {
        "job_id": job["job_id"],
        "university_id": university_id or job.get("card_id", ""),
        "task_type": "refresh_officials",
        "status": status_map.get(job["status"], JobStatusEnum.FAILED),
        "progress": job.get("progress", 0),
        "message": job.get("message", "Задание выполняется"),
        "created_at": job.get("created_at", ""),
        "completed_at": job.get("completed_at"),
        "result": job.get("result"),
    }


@router.post(
    "/universities/{university_id}/refresh",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Запустить AI-сбор и проверку сведений в Supabase-карточке",
)
async def trigger_university_refresh(university_id: UUID, background_tasks: BackgroundTasks):
    from app.api.v1.ai import checked_async, collect_job, get_collector, get_pipeline, get_repository

    repository = get_repository()
    card_id = str(university_id)
    card = await checked_async(lambda: repository.card(card_id))
    pipeline, collector = get_pipeline(), get_collector()
    source_options = {"include_institution": True, "public_site_only": True}
    request_key = f"legacy-refresh:{uuid4().hex}"
    job, created = repository.new_job(card_id, request_key, "collect", {
        "version": card["version"], "leadership_url": None, **source_options,
    })
    if created:
        background_tasks.add_task(collect_job, repository, pipeline, collector,
                                  card, job["job_id"], source_options=source_options)
    return _job_response(job, card_id)


@router.get("/jobs/{job_id}", response_model=JobResponse, summary="Проверить статус фоновой задачи")
async def get_job_status(job_id: str):
    from app.api.v1.ai import get_repository

    try:
        job = get_repository().job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return _job_response(job)
