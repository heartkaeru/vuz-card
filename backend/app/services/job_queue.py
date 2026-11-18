import asyncio
import uuid
from datetime import datetime
from typing import Dict, Any

from app.schemas.job import JobResponse, JobStatusEnum
from app.data.mock_universities import MOCK_UNIVERSITIES_DB

# Хранилище фоновых задач в памяти
JOBS_STORE: Dict[str, Dict[str, Any]] = {}


async def run_create_pipeline(
    job_id: str,
    university_id: int,
    name: str,
    city: str,
    website: str,
):
    """Фоновое создание карточки: парсер сам находит сайт и ректора."""

    try:
        JOBS_STORE[job_id]["progress"] = 15
        JOBS_STORE[job_id]["message"] = "Парсер: поиск официального сайта и ректора..."

        from app.services.parser_service import build_card, to_officials

        card = await asyncio.to_thread(build_card, name, city, website)

        JOBS_STORE[job_id]["progress"] = 75
        JOBS_STORE[job_id]["message"] = "Парсер: перенос фактов в карточку..."

        uni = MOCK_UNIVERSITIES_DB.get(university_id)
        if uni is None:
            raise RuntimeError(f"Карточка id={university_id} не найдена")

        site = card.university.url or website
        uni["website"] = site
        uni["city"] = card.university.city or city or uni.get("city", "")
        uni["region"] = card.university.region or uni.get("region", "")
        uni["officials"] = to_officials(card, institution_id=str(university_id))
        rector = next(
            (
                leader.full_name
                for leader in card.leadership
                if "ректор" in (leader.position or "").lower()
                and "проректор" not in (leader.position or "").lower()
            ),
            None,
        )
        uni["rector_fio"] = rector or uni.get("rector_fio")
        uni["active_projects"] = [
            project.name for project in card.projects[:20]
        ]
        uni["updated_at"] = datetime.now().isoformat()
        uni["version"] += 1

        JOBS_STORE[job_id]["progress"] = 100
        JOBS_STORE[job_id]["status"] = JobStatusEnum.COMPLETED
        JOBS_STORE[job_id]["message"] = "Карточка создана и заполнена парсером."
        JOBS_STORE[job_id]["completed_at"] = datetime.now().isoformat()
        JOBS_STORE[job_id]["result"] = {
            "university_id": university_id,
            "website": site,
            "officials": len(uni["officials"]),
        }
    except Exception as exc:  # noqa: BLE001
        JOBS_STORE[job_id]["status"] = JobStatusEnum.FAILED
        JOBS_STORE[job_id]["message"] = f"Ошибка создания карточки: {exc}"
        JOBS_STORE[job_id]["completed_at"] = datetime.now().isoformat()
async def run_refresh_pipeline(job_id: str, university_id: int):
    """
    Фоновый воркер: пайплайн «Парсер → LLM → Обновление карточки».
    Выполняет шаги сбора данных, извлечения фактов и обновления карточки.
    """
    try:
        # Шаг 1: Старт парсера
        await asyncio.sleep(1.5)
        JOBS_STORE[job_id]["progress"] = 25
        JOBS_STORE[job_id]["message"] = "Парсер: сбор страниц ректората с официального сайта..."
        
        # Шаг 2: Очистка контента
        await asyncio.sleep(1.5)
        JOBS_STORE[job_id]["progress"] = 55
        JOBS_STORE[job_id]["message"] = "Очистка HTML и подготовка текста для нейросети..."
        
        # Шаг 3: Нейросеть (LLM / NER)
        await asyncio.sleep(2.0)
        JOBS_STORE[job_id]["progress"] = 85
        JOBS_STORE[job_id]["message"] = "GigaChat: извлечение должностных лиц, должностей и контактов..."
        
        # Шаг 4: Завершение и запись результата
        await asyncio.sleep(1.0)
        
        # Симулируем добавление свеженайденной персоналии со статусом «на проверке»
        if university_id in MOCK_UNIVERSITIES_DB:
            uni = MOCK_UNIVERSITIES_DB[university_id]
            # Добавим свежего проректора, найденного ИИ
            new_official = {
                "id": f"ai_found_{uuid.uuid4().hex[:6]}",
                "fio": "Смирнова Елена Александровна",
                "position": "Проректор по международным связям и молодежной политике",
                "department": "Управление международного сотрудничества",
                "responsibilities": "Координация программ академической мобильности, студенческие форумы и ассоциации.",
                "email": "smirnova.e@university.ru",
                "phone": "+7 (495) 000-11-22",
                "photo_url": None,
                "source_url": f"{uni['website']}/administration/international",
                "status": "ai_draft",
                "previous_value": "Новая должность, обнаруженная ИИ-пайплайном при актуализации сайта"
            }
            # Если такой еще нет, добавляем
            if not any(o["fio"] == new_official["fio"] for o in uni["officials"]):
                uni["officials"].append(new_official)
                uni["updated_at"] = datetime.now().isoformat()
                uni["version"] += 1

        JOBS_STORE[job_id]["progress"] = 100
        JOBS_STORE[job_id]["status"] = JobStatusEnum.COMPLETED
        JOBS_STORE[job_id]["message"] = "Актуализация успешно завершена. Найдены новые данные!"
        JOBS_STORE[job_id]["completed_at"] = datetime.now().isoformat()
        
    except Exception as e:
        JOBS_STORE[job_id]["status"] = JobStatusEnum.FAILED
        JOBS_STORE[job_id]["message"] = f"Ошибка выполнения задачи: {str(e)}"
        JOBS_STORE[job_id]["completed_at"] = datetime.now().isoformat()
