from typing import Optional
from pydantic import BaseModel, Field


class PresentationCreateRequest(BaseModel):
    """Параметры для запуска сборки презентации"""
    template: str = Field(default="sber_standard", description="Идентификатор дизайн-шаблона")
    include_biography: bool = Field(default=True, description="Включать ли расширенную биографию ректора")
    include_projects: bool = Field(default=True, description="Включать ли слайд по совместным проектам")


class PresentationResponse(BaseModel):
    """Сведения о сформированной презентации"""
    presentation_id: str = Field(..., description="Идентификатор презентации")
    university_id: int = Field(..., description="ID ВУЗа")
    title: str = Field(..., description="Название презентации")
    download_url: str = Field(..., description="Прямая ссылка на скачивание файла .pptx")
    created_at: str = Field(..., description="Дата формирования")
