from typing import Optional, List, Dict, Any
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


class SlideContent(BaseModel):
    """Один слайд презентации для интерактивного предпросмотра в браузере"""
    slide_number: int = Field(..., description="Порядковый номер слайда (1, 2, ...)")
    slide_type: str = Field(..., description="Тип слайда: 'title', 'officials', 'summary'")
    title: str = Field(..., description="Заголовок слайда")
    subtitle: Optional[str] = Field(None, description="Подзаголовок слайда")
    elements: Dict[str, Any] = Field(
        ..., 
        description="Структурированные данные слайда (плашки, контакты, списки) для вёрстки"
    )


class PresentationPreviewResponse(BaseModel):
    """Полная структура презентации для просмотра в модальном окне CRM перед скачиванием"""
    university_id: int = Field(..., description="ID ВУЗа")
    university_name: str = Field(..., description="Название ВУЗа")
    total_slides: int = Field(..., description="Общее количество слайдов")
    slides: List[SlideContent] = Field(..., description="Список слайдов для карусели/просмотра")
