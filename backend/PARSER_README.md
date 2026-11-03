# Парсер «Карточка ВУЗа»

Модуль сбора и очистки данных с официальных сайтов вузов. Готовит:

1. `UniversityData` — структурированную карточку вуза.
2. `SourceDocument` — очищенные документы с фрагментами и контактными
   подсказками, совместимые с LLM-контрактами `app.services.llm`.

## Запуск

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
data/output/<slug>/<slug>_changes_<timestamp>.json
data/parser.sqlite3
data/cache/pages.json
data/logs/parser.log
```

`data/` исключён из git: результаты парсинга не коммитятся.

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

## Тесты

```bash
cd backend
python -m pytest tests/test_parser.py tests/test_cleaner.py
```
