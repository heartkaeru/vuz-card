export type ApiCard = { id: string; name: string; city: string; website: string; version: number; latest_run: string | null };
export type Fact = {
  fact_id: string; field: string; value: string;
  subject: {
    person_name: string | null; entity_name?: string; entity_id?: string;
    entity_kind?: "institution" | "project" | "event"; department?: string | null; position?: string | null;
  };
  origin?: "llm" | "subject_projection" | "parser_projection" | "curated_candidate";
  contact_owner?: { kind: string; label: string } | null;
  metric_context?: { period: string; scope: string; unit: string; coverage?: string | null } | null;
  verification_status: "pending" | "verified" | "conflict" | "rejected";
  validation_status: "passed" | "failed"; issues: string[];
  evidence: { url: string; quote: string; retrieved_at: string; page_number?: number | null; file_sha256?: string | null }[];
  last_checked_at?: string | null;
};
export type ResearchRun = {
  run_id: string; revision: number; status: string; facts: Fact[];
  parser_errors: string[]; parser_warnings: string[]; errors: string[];
  results: Record<string, { gaps: string[]; errors: { code: string }[]; run?: { provider: string } }>;
  previous_run_id?: string | null; baseline_facts?: Fact[]; retired_fact_ids?: string[];
};
export type FactChange = { change_id: string; kind: "new" | "changed" | "head_change" | "unchanged" | "not_confirmed";
  field: string; subject_label: string; before: Fact[]; after: Fact[]; retirement_ids: string[]; retirement_trigger_ids: string[] };
export type ChangeSet = { run_id: string; revision: number; previous_run_id: string | null; entries: FactChange[];
  counts: Record<FactChange["kind"], number>; approved_facts: Fact[]; warning: string };
export type ResearchHistory = { run_id: string; revision: number; created_at: string; status: string; approved_count: number; candidate_count: number };
export type PresentationVariant = "compact" | "detailed";
export type Material = { material_id: string; download_url: string; brief_url: string; presentation_variant?: PresentationVariant };
export type SavedMaterial = { id: string; name: string; format: "PPTX"; created_at: string; stale: boolean; download_url: string; brief_url: string };
export type Job = {
  job_id: string; status: string; task_type: string; progress: number; message: string;
  identity?: { expected_revision?: number; leadership_url?: string | null; biography_url?: string | null;
    institution_urls?: string[]; pdf_sources?: {url: string; pages: number[] | null}[];
    include_institution?: boolean; meeting?: { goal: string; date: string | null }; presentation_variant?: PresentationVariant };
  result: ({ run_id?: string; code?: string } & Partial<Material>) | null;
};
export type CardDetail = ApiCard & { run: ResearchRun | null; job: Job | null };

const errors: Record<string, string> = {
  gigachat_configuration_required: "На сервере не настроен доступ к GigaChat. Проверьте backend/.env и перезапустите сервер.",
  no_accepted_facts: "Сначала проверьте и примите факты по источникам.",
  stale_run_revision: "Данные уже изменились. Обновите карточку и повторите действие.",
  card_job_already_running: "Для этой карточки уже выполняется задание.",
  facts_changed_during_generation: "Во время генерации факты изменились. Сформируйте материалы заново.",
  source_check_required: "Подтвердите проверку выбранных фактов по первоисточникам.",
  explicit_replacement_confirmation_required: "Изменения уже пересчитаны или не подтверждена замена прежних сведений. Обновите сравнение и подтвердите замену.",
  replacement_source_check_required: "Проверьте источники прежних и новых значений перед заменой.",
  ambiguous_head_replacement: "Найдено несколько вариантов руководства. Сначала отклоните неподходящих кандидатов и проверьте сравнение повторно.",
  no_valid_extraction: "Проверьте сайт или укажите точную страницу руководства. Выдуманные сведения не подставляются.",
  gigachat_http_401: "Не удалось авторизоваться в GigaChat. Проверьте ключ на сервере.",
  gigachat_http_402: "Недостаточно доступных токенов GigaChat. Проверьте лимит аккаунта.",
  gigachat_http_429: "GigaChat временно ограничил запросы. Повторите позже.",
  gigachat_timeout: "GigaChat не ответил за отведённое время. Повторите запрос.",
  gigachat_connection_failed: "Сервер не смог подключиться к GigaChat. Проверьте сеть.",
  gigachat_certificate_verification_failed: "Серверу нужен доверенный сертификат для подключения к GigaChat.",
  server_restarted: "Сервер перезапущен. Запустите задание заново.",
  resolve_conflict_or_validation_before_acceptance: "Конфликтующие или ошибочные факты нельзя принять без уточнения.",
  leadership_source_must_belong_to_official_domain: "Источники должны находиться на официальном сайте учреждения.",
  research_superseded_by_new_collection: "Есть более новый результат сбора. Откройте его для проверки.",
  chunk_limit_exceeded: "Обработана только часть текста: достигнут лимит фрагментов. Укажите более точную страницу руководства.",
  fragment_too_large: "Один из блоков источника слишком большой для обработки. Нужна более точная разметка страницы.",
  invalid_parser_llm_contract: "Разметка источника не прошла проверку. Нужна проверка фрагментов и владельцев контактов в парсере.",
  invalid_extraction_json: "Модель вернула ответ, не соответствующий формату фактов. Часть сведений не извлечена.",
  no_source_documents: "Парсер не получил подходящих текстов. Укажите точную страницу руководства.",
  empty_source_documents: "Часть страниц не содержит пригодного текста или фрагментов.",
  pdf_download_failed: "Не удалось скачать PDF. Проверьте адрес и доступность отчёта.",
  pdf_text_not_found_ocr_required: "В PDF нет извлекаемого текста. Для сканов потребуется OCR.",
  pdf_pages_without_text_ocr_required: "Часть выбранных страниц не содержит текста; возможен скан.",
  invalid_pdf_page_selection: "Выберите до 20 существующих страниц PDF (нумерация с 1).",
  pdf_page_limit_exceeded: "PDF содержит больше 300 страниц. Нужен меньший документ.",
  pdf_text_limit_exceeded_select_fewer_pages: "Выбрано слишком много текста PDF. Укажите меньше страниц.",
  encrypted_pdf_not_supported: "Защищённый паролем PDF не поддерживается.",
  invalid_pdf_signature: "По ссылке получен не PDF. Проверьте прямой адрес файла.",
  pdf_file_too_large: "PDF превышает установленный лимит загрузки.",
  incomplete_model_response: "Ответ модели прерван. Часть сведений не обработана; повторите сбор или уточните источник.",
};

export function errorText(error: unknown) {
  return error instanceof Error ? errors[error.message] ?? (/^[a-z0-9_:.-]+$/.test(error.message)
    ? "Задание остановлено. Проверьте источники и повторите действие." : error.message) : "Не удалось выполнить действие.";
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1/ai${path}`, { ...options, cache: "no-store", headers: {
    "Content-Type": "application/json", ...options?.headers,
  }});
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : "Проверьте введённые данные и доступность сервера.");
  }
  return response.json() as Promise<T>;
}

export const post = <T>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) });
