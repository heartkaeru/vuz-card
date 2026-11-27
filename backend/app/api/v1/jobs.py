"""Compatibility route for checking background task status."""

from fastapi import APIRouter, HTTPException

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


@router.get("/jobs/{job_id}", response_model=JobResponse, summary="Проверить статус фоновой задачи")
async def get_job_status(job_id: str):
    from app.api.v1.ai import get_repository

    try:
        job = get_repository().job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return _job_response(job)
