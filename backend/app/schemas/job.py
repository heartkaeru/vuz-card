from enum import Enum
from typing import Optional, Any, Dict
from pydantic import BaseModel, Field


class JobStatusEnum(str, Enum):
    PENDING = "pending"          # Задача создана и ждет очереди
    IN_PROGRESS = "in_progress"  # Парсер или ИИ прямо сейчас работает
    COMPLETED = "completed"      # Успешно завершено
    FAILED = "failed"            # Произошла ошибка


class JobResponse(BaseModel):
    """Информация о статусе фоновой задачи (парсер, ИИ или сборка презентации)"""
    job_id: str = Field(..., description="Уникальный идентификатор задачи")
    university_id: int = Field(..., description="ID ВУЗа, к которому относится задача")
    task_type: str = Field(..., description="Тип задачи: refresh_officials или generate_presentation")
    status: JobStatusEnum = Field(..., description="Текущее состояние выполнения")
    progress: int = Field(default=0, ge=0, le=100, description="Прогресс выполнения от 0 до 100%")
    message: str = Field(..., description="Понятное текстовое описание текущего шага")
    created_at: str = Field(..., description="Время запуска задачи")
    completed_at: Optional[str] = Field(None, description="Время завершения")
    result: Optional[Dict[str, Any]] = Field(None, description="Результат работы (например, новые персоналии)")
