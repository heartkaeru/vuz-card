from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Literal
from pydantic import BaseModel, Field, HttpUrl, model_validator


class VerificationStatus(str, Enum):
    """Статус проверки данных человека"""
    VERIFIED = "verified"          # Подтверждено сотрудником вручную
    AI_DRAFT = "ai_draft"          # Свежие данные, найденные ИИ (требуют проверки)
    NEEDS_REVIEW = "needs_review"  # Обнаружены расхождения («было / стало»)
    UNVERIFIED = "unverified"
    REJECTED = "rejected"


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
    # Поля для поддержки экрана «было / стало»
    previous_value: Optional[str] = Field(
        None, 
        description="Предыдущее значение (если ИИ обнаружил изменения)"
    )
    version: int = 1
    appointment_status: Literal["current", "former", "unknown"] = "unknown"


class UniversityBase(BaseModel):
    """Базовая информация о ВУЗе (для списков и поиска)"""
    id: str
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
    metrics: list["UniversityMetric"] = Field(default_factory=list)


class UniversityUpdate(BaseModel):
    """Поля для ручного редактирования карточки (метод PATCH)"""
    name: Optional[str] = Field(None, min_length=2, max_length=500)
    short_name: Optional[str] = None
    website: Optional[HttpUrl] = None
    region: Optional[str] = None
    city: Optional[str] = None
    reported_inn: Optional[str] = Field(None, pattern=r"^$|^[0-9]{10}$|^[0-9]{12}$")
    profile: Optional[str] = None
    institution_type: Optional[Literal["university", "spo", "mixed", "unknown"]] = None
    verification_status: Optional[VerificationStatus] = None
    photo_url: Optional[str] = None
    version: int = Field(..., description="Текущая версия карточки для защиты от перезаписи")


class UniversityCreateRequest(BaseModel):
    """Создание карточки по названию: ссылка на сайт не требуется."""

    name: str = Field(..., min_length=2, description="Название вуза или колледжа")
    city: Optional[str] = Field(None, description="Город для уточнения поиска")
    website: Optional[HttpUrl] = Field(
        None, description="Необязательно: если ссылка уже известна"
    )
    short_name: Optional[str] = None
    region: Optional[str] = None
    inn: Optional[str] = Field(None, pattern=r"^[0-9]{10}$|^[0-9]{12}$")
    profile: Optional[str] = None
    institution_type: Literal["university", "spo", "mixed", "unknown"] = "university"


class UniversityResolveResponse(BaseModel):
    """Результат автоопределения официального сайта."""

    name: str
    website: str
    city: Optional[str] = None


class OfficialCreate(BaseModel):
    fio: str = Field(min_length=2, max_length=300)
    position: str = Field(min_length=2, max_length=300)
    department: Optional[str] = None
    responsibilities: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    photo_url: Optional[str] = None
    source_url: Optional[str] = None
    status: VerificationStatus = VerificationStatus.NEEDS_REVIEW
    appointment_status: Literal["current", "former", "unknown"] = "unknown"


class OfficialUpdate(BaseModel):
    fio: Optional[str] = Field(None, min_length=2, max_length=300)
    position: Optional[str] = Field(None, min_length=2, max_length=300)
    department: Optional[str] = None
    responsibilities: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    photo_url: Optional[str] = None
    source_url: Optional[str] = None
    status: Optional[VerificationStatus] = None
    appointment_status: Optional[Literal["current", "former", "unknown"]] = None


class MetricCreate(BaseModel):
    metric_code: str = Field(min_length=1, max_length=100)
    value: Decimal = Field(ge=0)
    unit: str = Field(min_length=1, max_length=80)
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    period_kind: Literal["month", "quarter", "year", "as_of", "unknown"] = "unknown"
    aggregation_kind: Literal["period_total", "ytd", "snapshot", "average", "unknown"] = "unknown"
    scope_code: str = "unknown"
    methodology: str = "unknown"
    scenario: Literal["actual", "plan", "estimate", "reported"] = "reported"
    source_as_of: Optional[datetime] = None
    raw_value: str = ""
    verification_status: VerificationStatus = VerificationStatus.NEEDS_REVIEW
    source_url: Optional[str] = None

    @model_validator(mode="after")
    def validate_period(self):
        if (self.period_start is None) != (self.period_end is None):
            raise ValueError("period_start_and_end_must_be_set_together")
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError("period_end_before_period_start")
        if self.verification_status == VerificationStatus.VERIFIED and (
            not self.period_start or self.scope_code == "unknown" or self.methodology == "unknown"
            or self.aggregation_kind == "unknown"
        ):
            raise ValueError("verified_metric_requires_period_scope_and_methodology")
        return self


class MetricUpdate(BaseModel):
    metric_code: Optional[str] = Field(None, min_length=1, max_length=100)
    value: Optional[Decimal] = Field(None, ge=0)
    unit: Optional[str] = Field(None, min_length=1, max_length=80)
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    period_kind: Optional[Literal["month", "quarter", "year", "as_of", "unknown"]] = None
    aggregation_kind: Optional[Literal["period_total", "ytd", "snapshot", "average", "unknown"]] = None
    scope_code: Optional[str] = None
    methodology: Optional[str] = None
    scenario: Optional[Literal["actual", "plan", "estimate", "reported"]] = None
    source_as_of: Optional[datetime] = None
    raw_value: Optional[str] = None
    verification_status: Optional[VerificationStatus] = None
    source_url: Optional[str] = None


class UniversityMetric(MetricCreate):
    id: str
    university_id: str
    version: int = 1


UniversityDetail.model_rebuild()
