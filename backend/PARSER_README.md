# Парсер «Карточка ВУЗа»

Модуль сбора и очистки данных с официальных сайтов вузов. Готовит:

1. `UniversityData` — структурированную карточку вуза.
2. `SourceDocument` — очищенные документы с фрагментами и контактными
   подсказками, совместимые с LLM-контрактами `app.services.llm`.

## Запуск

Дополнение Демо 1: официальные текстовые PDF можно передать через `pdf_sources`
серверного сценария. Номера выбранных страниц и хеш файла сохраняются вместе с
цитатами; PDF/сканы не превращаются в HTML. Подробнее —
[`DOSSIER_PIPELINE.md`](docs/llm/DOSSIER_PIPELINE.md#pdf-и-предпросмотр).

Из каталога `backend/`:

```bash
python -m parser.main --profile hse
python -m parser.main --profile urfu
python -m parser.main --all
```

Из корня репозитория:

```bash
python -m backend.parser.main --profile hse
```

Произвольный сайт или список:

```bash
python -m parser.main --url https://example.ru --name "Пример"
python -m parser.main --input-file universities.txt
```

Форматы `--input-file`: URL, `Название, URL`, CSV/TSV или JSON-список
`{"name": "...", "url": "..."}`.

## Профили

`spbu`, `hse`, `rsuh`, `urfu`. Профиль задаёт проверенные URL разделов;
для остальных сайтов разделы ищутся автоматически по меню, ссылкам,
`sitemap.xml` и ключевым словам.

## Результаты

По умолчанию:

```text
data/output/<slug>/<slug>_card.json
data/output/<slug>/<slug>_documents_<timestamp>.json
data/output/<slug>/<slug>_llm_card_<timestamp>.json
data/output/<slug>/<slug>_changes_<timestamp>.json
data/parser.sqlite3
data/cache/pages.json
data/logs/parser.log
```

`data/` исключён из git: результаты парсинга не коммитятся.

`*_llm_card_*.json` — полный нативный `UniversityData`, включая документы и фрагменты,
для [LLMWorkflow / workflow_cli](docs/llm/LLM_WORKFLOW.md). Старый `*_card.json`
не содержит документов; описательный `*_documents_*.json` не является этим входным контрактом.
В серверном сценарии Демо 1 `UniversityParser.parse(..., allowed_sections=...)`
ограничивает сбор руководством/контактами. При проблемах автообнаружения UI позволяет
передать точную страницу руководства на том же официальном домене.

## Категории документов

`organization`, `leadership`, `contacts`, `research`, `projects`, `grants`,
`project_news`, `partnerships`, `achievements`, `other`.

Документ в файле `*_documents_*.json` имеет вид:

```json
{
  "university": {"name": "...", "official_domain": "university.ru"},
  "document": {
    "category": "project_news",
    "title": "...",
    "url": "...",
    "published_at": "2026-11-03",
    "collected_at": "2026-11-03T15:00:00Z",
    "language": "ru",
    "content": "...",
    "content_hash": "sha256...",
    "content_type": "text/html",
    "fragments": []
  }
}
```

## Интеграция с LLM-модулем

Адаптер: `parser/llm_adapter.py`. Он не импортирует `app`, поэтому может
использоваться и в API, и в отдельных скриптах.

```python
from parser.llm_adapter import to_llm_request, to_officials

card = parser.parse(profile="hse")

# Schema 0.2: руководство и контакты с фрагментами.
request = to_llm_request(card, institution_id="hse", card_version=1)

# Schema 0.3: профиль, проекты и события.
context_request = to_llm_request(
    card, institution_id="hse", schema_version="0.3"
)

# Формат app.schemas.university.Official
officials = to_officials(card, institution_id="hse")
```

Полученный `request` валидируется контрактами
`app.services.llm.fragment_models.FragmentExtractionRequest` (0.2) или
`app.services.llm.context_models.ContextExtractionRequest` (0.3).

Контакт без подтверждённого структурой страницы или заголовком владельца остаётся в тексте, но не
превращается в структурированную подсказку с выдуманным владельцем.
Карточки с microdata `rucovodstvo` / `rucovodstvoZam` сохраняются отдельными
фрагментами; `fio` задаёт владельца рабочих контактов. В `rucovodstvoFil`
владельцем контакта считается филиал `nameFil`, а не директор. Вложенные карточки
разделяются. Обозначенные внутри личной карточки контакты приёмной/секретаря не
переносятся на человека. Добавочный номер до четырёх цифр сохраняется дословно,
если общий номер удовлетворяет ограничению контракта 0.2 (до 15 цифр).
Общие заголовки «Руководство» и «Руководитель и заместители» не задают владельца.
В простых microdata-таблицах с заголовком ФИО и без колонок подразделений контакты
привязываются к человеку. Справочники с явными колонками подразделения и email
читаются отдельно, включая однозначные `rowspan`; почта принадлежит подразделению.
Колонки положений/подписей документов не включаются в текст такого справочника,
чтобы ФИО подписанта не становилось текущим назначением. Общий телефон справочной
службы не приписывается подразделению по соседству. Сложные `colspan`, неверная
сетка и неоднозначная область остаются без автоматической разметки владельца.
`subject_department` передаёт дословное название филиала/подразделения в 0.2-контракт;
оно должно присутствовать в том же фрагменте. Неизвестный владелец не угадывается.
При чтении HTML из кэша `retrieved_at` сохраняет дату загрузки страницы;
повторная очистка не подтверждает актуальность сайта. В `metadata.cache_status`
отмечается использование сохранённой или устаревшей копии.

В режиме 0.3 весь документ не считается одной областью сущности. По умолчанию
фрагменты независимы: это предотвращает перенос статуса между разными проектами
на странице списка. Если парсер надёжно выделил одну статью/проект, его фрагментам
можно явно задать одинаковый `DocumentFragment.entity_scope_id`. Без такой
разметки связи между разными абзацами потребуют дополнительной обработки.

## Тесты

```bash
cd backend
python -m pytest tests/test_parser.py tests/test_cleaner.py
```

Общая проверка LLM, парсера и Excel-импорта из корня репозитория:

```powershell
backend/.venv/Scripts/python.exe -m pytest -q
```
