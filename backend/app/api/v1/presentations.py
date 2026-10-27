import os
import uuid
from datetime import datetime
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from app.schemas.presentation import PresentationCreateRequest, PresentationResponse
from app.data.mock_universities import MOCK_UNIVERSITIES_DB
from app.services.pptx_builder import build_university_presentation, STORAGE_DIR

router = APIRouter(tags=["Генерация презентаций"])

# Хранилище сгенерированных презентаций
PRESENTATIONS_STORE = {}


@router.post(
    "/universities/{university_id}/presentations", 
    response_model=PresentationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Сформировать презентацию к встрече (.pptx)"
)
def generate_presentation(university_id: int, request: PresentationCreateRequest = None):
    """
    Генерирует PowerPoint-презентацию (.pptx) по шаблону
    с подстановкой проверенных данных из карточки ВУЗа.
    """
    if university_id not in MOCK_UNIVERSITIES_DB:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ВУЗ с id={university_id} не найден"
        )
        
    try:
        file_path = build_university_presentation(university_id)
        pres_id = f"pres_{uuid.uuid4().hex[:8]}"
        uni = MOCK_UNIVERSITIES_DB[university_id]
        
        info = {
            "presentation_id": pres_id,
            "university_id": university_id,
            "title": f"Материалы к встрече: {uni['short_name']}",
            "file_path": file_path,
            "download_url": f"/api/v1/presentations/{pres_id}/download",
            "created_at": datetime.now().isoformat()
        }
        PRESENTATIONS_STORE[pres_id] = info
        
        return PresentationResponse(
            presentation_id=info["presentation_id"],
            university_id=info["university_id"],
            title=info["title"],
            download_url=info["download_url"],
            created_at=info["created_at"]
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Не удалось сформировать презентацию: {str(e)}"
        )


@router.get(
    "/presentations/{presentation_id}/download", 
    summary="Скачать сформированную презентацию"
)
def download_presentation(presentation_id: str):
    """
    Отдаёт сгенерированный файл .pptx клиенту для скачивания.
    """
    if presentation_id not in PRESENTATIONS_STORE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Презентация не найдена или устарела"
        )
        
    pres_info = PRESENTATIONS_STORE[presentation_id]
    file_path = pres_info["file_path"]
    
    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Файл на сервере не найден"
        )
        
    return FileResponse(
        path=file_path,
        filename=os.path.basename(file_path),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
