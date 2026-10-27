from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field


class VerificationStatus(str, Enum):
    """Статус проверки данных человека"""
    VERIFIED = "verified"          # Подтверждено сотрудником вручную
    AI_DRAFT = "ai_draft"          # Свежие данные, найденные ИИ (требуют проверки)
    NEEDS_REVIEW = "needs_review"  # Обнаружены расхождения («было / стало»)


class Official(BaseModel):
    """Должностное лицо ВУЗа (Ректор, Проректор, Руководитель направления)"""
    id: str = Field(..., description="Уникальный идентификатор персоналии")
    fio: str = Field(..., description="ФИО руководителя")
    position: str = Field(..., description="Должность (Ректор, Проректор по науке и т.д.)")
    department: Optional[str] = Field(None, description="Подразделение или направление работы")
    responsibilities: Optional[str] = Field(None, description="Краткое описание зоны ответственности")
    email: Optional[str] = Field(None, description="Рабочий email")
    phone: Optional[str] = Field(None, description="Контактный телефон")
    photo_url: Optional[str] = Field(None, description="Ссылка на фотографию")
    source_url: Optional[str] = Field(None, description="Ссылка на страницу официального сайта вуза")
    status: VerificationStatus = Field(
        default=VerificationStatus.VERIFIED, 
        description="Статус верификации данных"
    )
    # Поля для поддержки экрана «было / стало» (по дизайну Ангелины)
    previous_value: Optional[str] = Field(
        None, 
        description="Предыдущее значение (если ИИ обнаружил изменения)"
    )


class UniversityBase(BaseModel):
    """Базовая информация о ВУЗе (для списков и поиска)"""
    id: int
    name: str = Field(..., description="Полное наименование учебного заведения")
    short_name: str = Field(..., description="Краткое название / аббревиатура")
    inn: str = Field(..., description="ИНН учебного заведения (ключ для объединения баз)")
    website: str = Field(..., description="Официальный сайт")
    region: str = Field(..., description="Субъект РФ")
    city: str = Field(..., description="Город")
    rector_fio: Optional[str] = Field(None, description="ФИО действующего ректора")
    version: int = Field(default=1, description="Версия карточки для оптимистичной блокировки")
    updated_at: str = Field(..., description="Дата последнего обновления данных")


class UniversityDetail(UniversityBase):
    """Полная карточка ВУЗа со списком руководства и показателями"""
    officials: List[Official] = Field(default_factory=list, description="Список ключевых должностных лиц")
    fot_volume: Optional[str] = Field(None, description="Объем фонда оплаты труда (ФОТ)")
    contracts_count: Optional[int] = Field(0, description="Количество действующих договоров")
    active_projects: List[str] = Field(
        default_factory=list, 
        description="Участие в федеральных программах (Приоритет-2030, ПИШ и др.)"
    )


class UniversityUpdate(BaseModel):
    """Поля для ручного редактирования карточки (метод PATCH)"""
    short_name: Optional[str] = None
    website: Optional[str] = None
    city: Optional[str] = None
    fot_volume: Optional[str] = None
    version: int = Field(..., description="Текущая версия карточки для защиты от перезаписи")
