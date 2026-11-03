# Связка парсера, LLM и БД

`LLMWorkflow` связывает существующие контракты с SQLite: сохранённый вход парсера →
кандидаты → явное решение сотрудника → материалы только по принятому снимку.
Для UI [Демо 1](../../../DEMO1_RUNBOOK.md) добавлен `app/api/v1/ai.py`.

Основные объекты: `database.llm_store.LLMRepository`,
`app.services.llm.workflow.LLMWorkflow`, `workflow_models.ResearchRun/ReviewBatch`.
`DemoRepository` добавляет черновые карточки, статусы заданий и список реальных файлов.

```python
repository = LLMRepository(database_path)
workflow = LLMWorkflow(repository, AssistantPipeline(provider))
research, created = workflow.create(institution, parser_card, request_key)
if research.status == "queued":
    research = await workflow.extract(research.run_id)
# После проверки источников сотрудником:
research = workflow.review(research.run_id, review_batch, actor=authenticated_actor)
material_id, draft = await workflow.generate(research.run_id, research.revision)
```

`institution.id` существующего реестра требует соответствующих имени и версии.
Неподтверждённое учреждение имеет явно отдельный `draft:<local-id>`; юридические лица
и ИНН не создаются. Версия карточки и `revision` исследования — разные величины.
Автор review должен поступать из контекста пользователя backend; CLI/локальное UI
пока только записывают введённое имя и не заменяют будущую аутентификацию.

Данные сохраняются дополнительными таблицами `llm_*`, без изменения таблиц импорта/
юридических лиц и `PRAGMA user_version`. Исходный `UniversityData` сохраняется отдельно
от результатов/проверенных копий. Вход, история решений и снимок фактов материала
защищены от изменения/удаления SQL-триггерами. Это дополнение локального прототипа;
миграции при развитии схемы нужно согласовать с владельцем БД.

Повтор `(institution.id, request_key)` с тем же входом возвращает исследование;
другой вход под тем же ключом отклоняется. Начатый сбор не запускается повторно неявно.
Новые попытки после ошибки используют новый ключ; до начала извлечения `queued`
можно продолжить. Ошибки/пустые источники/частичные результаты сохраняются.

Review требует `expected_revision`, решения с `fact_id`, `accept/reject`, основание и
`source_checked=true` для принятия. Сначала проверяется вся пачка; ошибка откатывает
все изменения. Модель не назначает автора/статус/время проверки. Некорректный факт и
неразрешённый конфликт не принимаются. Время записывает сервер. Истинность человеческого
утверждения о проверке источника модуль технически доказать не может.

Генерация выбирает факты из БД; неизвестные ID, непринятые/устаревшие значения,
изменённая версия карточки и конкурентное решение отклоняются. Проверка версии
повторяется перед сохранением. `llm_material_fact` фиксирует точные выбранные значения
для структурированного PPTX; исходный `ExtractionResult` не переписывается при review.
Смена активного исследования/решений делает старый файл архивным.

## CLI

Нужен `*_llm_card_*.json` — новый полный снимок, выдаваемый `parser.main.save_results`.
Он сохраняет тексты, ID, даты, области и подсказки контактов; обычный `*_card.json`
сводный, без документов, для workflow не подходит.

Из `backend`:

```powershell
.venv/Scripts/python.exe -m app.services.llm.workflow_cli --db output/mvp/registry.sqlite3 extract --input data/output/university/university_llm_card_TIMESTAMP.json --institution-id draft:local-university --request-key first-run --env-file .env --output output/research.json
.venv/Scripts/python.exe -m app.services.llm.workflow_cli --db output/mvp/registry.sqlite3 show RUN_ID --output output/research.json --review-template output/decisions.json
.venv/Scripts/python.exe -m app.services.llm.workflow_cli --db output/mvp/registry.sqlite3 review RUN_ID --decisions output/decisions.json --actor "Имя проверяющего"
.venv/Scripts/python.exe -m app.services.llm.workflow_cli --db output/mvp/registry.sqlite3 draft RUN_ID --revision REVISION --env-file .env --output-dir output/materials
```

Шаблон decisions содержит `decision=null`, `source_checked=false`, пустую причину:
он не исполняется, пока человек не заполнит реальные решения. `REVISION` берётся из
актуального исследования после review. `draft` выдаёт JSON/Markdown, HTTP-слой также PPTX.
Офлайн-ответы включаются только явным `--responses`. Код 2 означает частичный/ошибочный
сбор либо отказ в операции; код 1 — вход/конфигурация/доступ к файлу. Ключи и тела
ошибок SDK/валидации в CLI не печатаются.

Учебный сквозной пример без сети/API:
`python -m examples.llm.workflow_demo`. Он имитирует review исключительно на вымышленных
данных и подписывает это в консоли/артефактах; в рабочий процесс такое принятие не переносится.
