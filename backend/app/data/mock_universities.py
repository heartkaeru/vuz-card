"""
Тестовые данные (Mock) карточек ВУЗов.
Используются для автономной разработки фронтенда и тестирования API
до момента подключения базы данных PostgreSQL Максима.
"""

from typing import Dict, Any

MOCK_UNIVERSITIES_DB: Dict[int, Dict[str, Any]] = {
    1: {
        "id": 1,
        "name": "Московский государственный технический университет имени Н.Э. Баумана",
        "short_name": "МГТУ им. Н.Э. Баумана",
        "inn": "7701002520",
        "website": "https://bmstu.ru",
        "region": "г. Москва",
        "city": "Москва",
        "rector_fio": "Гордин Михаил Валерьевич",
        "version": 1,
        "updated_at": "2026-10-25T12:00:00",
        "fot_volume": "4.8 млрд руб.",
        "contracts_count": 14,
        "active_projects": ["Приоритет-2030", "Передовые инженерные школы (ПИШ)", "Цифровая кафедра"],
        "officials": [
            {
                "id": "bmstu_1",
                "fio": "Гордин Михаил Валерьевич",
                "position": "Ректор",
                "department": "Ректорат",
                "responsibilities": "Общее стратегическое руководство университетом, взаимодействие с органами государственной власти и ключевыми индустриальными партнёрами.",
                "email": "rector@bmstu.ru",
                "phone": "+7 (499) 263-63-91",
                "photo_url": "https://bmstu.ru/media/rector.jpg",
                "source_url": "https://bmstu.ru/about/administration/rector",
                "status": "verified",
                "previous_value": None
            },
            {
                "id": "bmstu_2",
                "fio": "Коршунов Сергей Валентинович",
                "position": "Первый проректор — проректор по учебной работе",
                "department": "Учебно-методическое управление",
                "responsibilities": "Организация образовательного процесса, разработка и аккредитация новых образовательных программ, цифровизация учебного плана.",
                "email": "korshunov@bmstu.ru",
                "phone": "+7 (499) 263-62-01",
                "photo_url": None,
                "source_url": "https://bmstu.ru/about/administration/vice-rectors",
                "status": "verified",
                "previous_value": None
            },
            {
                "id": "bmstu_3",
                "fio": "Зимин Сергей Евгеньевич",
                "position": "Проректор по цифровой трансформации",
                "department": "Центр цифрового развития",
                "responsibilities": "ИТ-инфраструктура кампуса, внедрение искусственного интеллекта в управление университетом, проект «Цифровой университет».",
                "email": "zimin@bmstu.ru",
                "phone": "+7 (499) 263-64-55",
                "photo_url": None,
                "source_url": "https://bmstu.ru/about/administration/vice-rectors",
                "status": "needs_review",
                "previous_value": "Ранее должность занимал: Черненький В.М. (ИИ обнаружил назначение нового проректора 15.10.2026)"
            }
        ]
    },
    2: {
        "id": 2,
        "name": "Национальный исследовательский университет «Высшая школа экономики»",
        "short_name": "НИУ ВШЭ",
        "inn": "7714030726",
        "website": "https://hse.ru",
        "region": "г. Москва",
        "city": "Москва",
        "rector_fio": "Анисимов Никита Юрьевич",
        "version": 1,
        "updated_at": "2026-10-24T18:30:00",
        "fot_volume": "8.2 млрд руб.",
        "contracts_count": 28,
        "active_projects": ["Приоритет-2030 (исследовательское лидерство)", "Исследовательский центр в сфере ИИ"],
        "officials": [
            {
                "id": "hse_1",
                "fio": "Анисимов Никита Юрьевич",
                "position": "Ректор",
                "department": "Ректорат",
                "responsibilities": "Стратегическое планирование, развитие кампусов в Москве, Санкт-Петербурге, Нижнем Новгороде и Перми.",
                "email": "rector@hse.ru",
                "phone": "+7 (495) 771-32-32",
                "photo_url": "https://hse.ru/media/anisimov.jpg",
                "source_url": "https://hse.ru/rector",
                "status": "verified",
                "previous_value": None
            },
            {
                "id": "hse_2",
                "fio": "Рощин Сергей Юрьевич",
                "position": "Проректор",
                "department": "Координация образовательной деятельности",
                "responsibilities": "Приёмная кампания, бакалавриат и магистратура, интеграция с бизнес-школами и работодателями.",
                "email": "sroshchin@hse.ru",
                "phone": "+7 (495) 772-95-90",
                "photo_url": None,
                "source_url": "https://hse.ru/org/persons/roshchin",
                "status": "verified",
                "previous_value": None
            }
        ]
    },
    3: {
        "id": 3,
        "name": "Санкт-Петербургский политехнический университет Петра Великого",
        "short_name": "СПбПУ",
        "inn": "7804040315",
        "website": "https://spbstu.ru",
        "region": "г. Санкт-Петербург",
        "city": "Санкт-Петербург",
        "rector_fio": "Рудской Андрей Иванович",
        "version": 1,
        "updated_at": "2026-10-22T09:15:00",
        "fot_volume": "3.9 млрд руб.",
        "contracts_count": 9,
        "active_projects": ["Приоритет-2030", "Передовые инженерные школы (ПИШ)"],
        "officials": [
            {
                "id": "spbstu_1",
                "fio": "Рудской Андрей Иванович",
                "position": "Ректор",
                "department": "Ректорат",
                "responsibilities": "Руководство университетом, взаимодействие с Минпромторгом и промышленными холдингами.",
                "email": "rector@spbstu.ru",
                "phone": "+7 (812) 552-60-80",
                "photo_url": None,
                "source_url": "https://spbstu.ru/structure/rectorate/",
                "status": "verified",
                "previous_value": None
            }
        ]
    }
}
