export type ApiCard = { id: string; name: string; city: string; website: string; version: number; latest_run: string | null };
export type CardIntake = { card: ApiCard; job: Job };
export type Fact = {
  fact_id: string; field: string; value: string;
  subject: {
    person_name: string | null; entity_name?: string; entity_id?: string;
    entity_kind?: "institution" | "project" | "event"; department?: string | null; position?: string | null;
  };
  origin?: "llm" | "subject_projection" | "parser_projection" | "curated_candidate" | "manual_edit";
  reviewed_by?: string | null; reviewed_at?: string | null;
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
  reconfirmed_facts?: Record<string, string[]>;
};
export type FactChange = { change_id: string; kind: "new" | "changed" | "head_change" | "unchanged" | "not_confirmed";
  field: string; subject_label: string; group_id: string; group_label: string;
  before: Fact[]; after: Fact[]; retirement_ids: string[]; retirement_trigger_ids: string[] };
export type ChangeSet = { run_id: string; revision: number; previous_run_id: string | null; entries: FactChange[];
  counts: Record<FactChange["kind"], number>; approved_facts: Fact[]; recheck_fact_ids?: string[];
  reconfirmed_facts?: Record<string, string[]>; warning: string };
export type ResearchHistory = { run_id: string; revision: number; created_at: string; status: string; approved_count: number; candidate_count: number };
export type PresentationVariant = "compact" | "detailed";
export type PptxPreviewParagraph = { text: string; font_family: string; font_size_pt: number; color: string | null; bold: boolean; alignment: string };
export type PptxPreviewShape =
  | { type: "image"; image_key: string; x: number; y: number; width: number; height: number }
  | { type: "text"; vertical_anchor: string; paragraphs: PptxPreviewParagraph[]; x: number; y: number; width: number; height: number };
export type PptxPreviewData = {
  width_inches: number; height_inches: number; images: Record<string, string>;
  slides: { shapes: PptxPreviewShape[] }[];
};
export type PartnershipMatch = {
  product_name: string; proposal: string; rationale: string; discussion_question: string;
  catalog_url: string; basis_fact_ids: string[]; kind: "sber_product" | "external_course";
};
export type Material = { material_id: string; download_url: string; brief_url: string; presentation_variant?: PresentationVariant };
export type SavedMaterial = { id: string; material_id: string; name: string; format: "PPTX" | "DOCX"; created_at: string; stale: boolean; download_url: string; preview_url: string; brief_url: string };
export type Job = {
  job_id: string; status: string; task_type: string; progress: number; message: string;
  identity?: { expected_revision?: number; leadership_url?: string | null; biography_url?: string | null;
    institution_urls?: string[]; pdf_sources?: {url: string; pages: number[] | null}[];
    include_institution?: boolean; meeting?: { goal: string; date: string | null }; presentation_variant?: PresentationVariant };
  result: ({ run_id?: string; code?: string } & Partial<Material>) | null;
};
export type CardDetail = ApiCard & { run: ResearchRun | null; job: Job | null };
export type CardActionResult = { message: string; queued?: boolean; needsRecheck?: boolean };

const errors: Record<string, string> = {
  gigachat_configuration_required: "На сервере не настроен доступ к GigaChat. Проверьте backend/.env и перезапустите сервер.",
  no_accepted_facts: "Сначала проверьте и примите факты по источникам.",
  material_stale_after_review: "Факты изменились. Сформируйте новую версию презентации.",
  material_not_found: "Презентация не найдена. Сформируйте ее заново.",
  material_file_not_found: "Файл презентации не найден. Сформируйте материалы заново.",
  report_not_found: "Отчет уже удален или недоступен. Обновите список отчетов.",
  "Not Found": "Backend не распознал новый обработчик удаления. Перезапустите backend и повторите.",
  "Method Not Allowed": "Backend не поддерживает удаление отчётов. Перезапустите backend и повторите.",
  presentation_preview_unavailable: "Предпросмотр презентации недоступен.",
  facts_require_freshness_check: "Истёк срок проверки фактов. Откройте блок «Повторная проверка перед экспортом» в карточке.",
  run_not_ready_for_generation: "Сбор сведений ещё не завершён. Откройте карточку и проверьте статус задания.",
  invalid_selected_fact_ids: "Состав принятых сведений изменился. Обновите карточку и повторите экспорт.",
  partnership_match_requires_selected_accepted_facts: "Основания для предложений изменились. Повторите подбор перед экспортом.",
  invalid_partnership_response: "Подборка не прошла проверку каталога и фактов. Повторите подбор.",
  chat_login_required: "Войдите, чтобы открыть диалог.",
  card_not_found: "Карточка уже недоступна. Обновите список вузов.",
  university_has_children: "Нельзя удалить карточку: к ней привязаны связанные записи.",
  chat_card_changed: "Карточка обновилась во время ответа. Отправьте вопрос ещё раз.",
  chat_history_changed: "В диалоге появилось новое сообщение. История обновлена. Отправьте вопрос ещё раз.",
  invalid_chat_tool_call: "Модель не смогла выбрать данные карточки. Уточните вопрос и повторите.",
  invalid_chat_reply: "Ответ не прошёл проверку ссылок на факты. Повторите вопрос.",
  chat_edit_value_required: "Укажите новое значение полностью в сообщении.",
  chat_edit_target_ambiguous: "Найдено несколько подходящих полей. Укажите владельца и текущее значение.",
  chat_edit_target_not_found: "Подходящее принятое поле не найдено. Уточните владельца или текущее значение.",
  chat_edit_invalid_value: "Проверьте формат нового телефона или адреса почты.",
  chat_edit_unchanged: "Это значение уже есть в карточке.",
  chat_edit_unknown_owner: "Сначала уточните владельца контакта в карточке.",
  chat_edit_not_found: "Предложенная правка не найдена.",
  chat_edit_not_pending: "Эта правка уже обработана.",
  chat_edit_collection_running: "Дождитесь завершения сбора и повторите запрос на правку.",
  chat_edit_requires_new_proposal: "Для изменения этого поля предложите новую правку в чате.",
  chat_collection_url_required: "Укажите один официальный URL в текущем сообщении.",
  chat_collection_name_required: "Укажите точное название учреждения в сообщении.",
  chat_collection_card_ambiguous: "В реестре несколько карточек с этим сайтом. Откройте нужную и запустите сбор из карточки.",
  public_website_required: "Для сбора укажите публичный официальный сайт учреждения.",
  official_site_not_found: "Не удалось найти официальный сайт по этому названию. Уточните название или укажите сайт вручную.",
  official_site_discovery_failed: "Не удалось выполнить поиск официального сайта. Повторите попытку позже или укажите сайт вручную.",
  collection_failed: "Сбор завершился ошибкой. Сервер сохранил технический журнал задания; проверьте доступность сайта и настройки ИИ.",
  collection_cancelled: "Сбор остановлен.",
  job_not_cancellable: "Это задание нельзя остановить этой кнопкой.",
  stale_run_revision: "Данные уже изменились. Обновите карточку и повторите действие.",
  fact_already_reconfirmed: "Часть выбранных сведений уже подтверждена повторным сбором. Снимите их выбор и повторите.",
  unknown_review_fact_id: "Часть выбранных сведений не относится к текущему сбору. Обновите карточку и выберите заново.",
  duplicate_review_fact_id: "Одни и те же сведения выбраны дважды. Обновите карточку и выберите заново.",
  invalid_retirement_fact_ids: "Не удалось определить, какие прежние сведения заменяются. Обновите сравнение и повторите.",
  run_not_reviewable: "Этот сбор ещё не завершён или уже недоступен для проверки. Дождитесь окончания сбора или обновите карточку.",
  review_actor_required: "Укажите проверяющего.",
  card_job_already_running: "Для этой карточки уже выполняется задание.",
  facts_changed_during_generation: "Во время генерации факты изменились. Сформируйте материалы заново.",
  source_check_required: "Подтвердите проверку выбранных фактов по первоисточникам.",
  fact_not_accepted: "Выбранные сведения уже не входят в принятые данные. Обновите карточку и повторите.",
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

export const BACKEND_CONNECTION_ERROR = "Не удалось подключиться к серверу. Убедитесь, что backend запущен.";

export function errorText(error: unknown) {
  if (error instanceof TypeError && /fetch|network/i.test(error.message)) return BACKEND_CONNECTION_ERROR;
  return error instanceof Error ? errors[error.message] ?? (/^[a-z0-9_:.-]+$/.test(error.message)
    ? `Не удалось выполнить действие (код: ${error.message}).` : error.message) : "Не удалось выполнить действие.";
}

export async function downloadFile(path: string, filename: string) {
  const response = await fetch(path, { cache: "no-store" });
  if (response.status === 401 && typeof window !== "undefined") {
    await fetch("/api/auth/logout", { method: "POST", cache: "no-store" }).catch(() => undefined);
    window.location.replace("/login");
    throw new Error("chat_login_required");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : "Не удалось скачать файл.");
  }
  const blobUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = blobUrl;
  link.download = filename.replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_");
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1/ai${path}`, { ...options, cache: "no-store", headers: {
    "Content-Type": "application/json", ...options?.headers,
  }});
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (response.status === 401 && typeof window !== "undefined") {
      await fetch("/api/auth/logout", { method: "POST", cache: "no-store" }).catch(() => undefined);
      window.location.replace("/login");
    }
    throw new Error(typeof body.detail === "string" ? body.detail : requestErrorText(response.status, body.detail));
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

/** Понятное сообщение, когда сервер вернул не код ошибки, а проверку полей или сбой. */
function requestErrorText(status: number, detail: unknown) {
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { loc?: unknown[]; msg?: string };
    const field = Array.isArray(first.loc) ? first.loc.filter((part) => typeof part === "string" && part !== "body").join(".") : "";
    return `Сервер не принял данные${field ? ` (поле ${field})` : ""}: ${first.msg ?? "проверьте введённые значения"}.`;
  }
  if (status >= 500) return `Внутренняя ошибка сервера (HTTP ${status}). Подробности — в журнале backend.`;
  return `Проверьте введённые данные и доступность сервера (HTTP ${status}).`;
}

export const post = <T>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) });
