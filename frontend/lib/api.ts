export type ApiCard = { id: string; name: string; city: string; website: string; version: number; latest_run: string | null };
export type Fact = {
  fact_id: string; field: string; value: string;
  subject: { person_name: string | null; entity_name?: string };
  contact_owner?: { kind: string; label: string } | null;
  verification_status: "pending" | "verified" | "conflict" | "rejected";
  validation_status: "passed" | "failed"; issues: string[];
  evidence: { url: string; quote: string; retrieved_at: string }[];
};
export type ResearchRun = {
  run_id: string; revision: number; status: string; facts: Fact[];
  parser_errors: string[]; errors: string[];
  results: Record<string, { gaps: string[]; errors: { code: string }[]; run?: { provider: string } }>;
};
export type Material = { material_id: string; download_url: string; brief_url: string };
export type SavedMaterial = { id: string; name: string; format: "PPTX"; created_at: string; stale: boolean; download_url: string; brief_url: string };
export type Job = {
  job_id: string; status: string; task_type: string; progress: number; message: string;
  identity?: { expected_revision?: number; leadership_url?: string | null };
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
  no_valid_extraction: "Проверьте сайт или укажите точную страницу руководства. Выдуманные сведения не подставляются.",
  gigachat_http_401: "Не удалось авторизоваться в GigaChat. Проверьте ключ на сервере.",
  gigachat_http_402: "Недостаточно доступных токенов GigaChat. Проверьте лимит аккаунта.",
  gigachat_http_429: "GigaChat временно ограничил запросы. Повторите позже.",
  gigachat_timeout: "GigaChat не ответил за отведённое время. Повторите запрос.",
  gigachat_connection_failed: "Сервер не смог подключиться к GigaChat. Проверьте сеть.",
  gigachat_certificate_verification_failed: "Серверу нужен доверенный сертификат для подключения к GigaChat.",
  server_restarted: "Сервер перезапущен. Запустите задание заново.",
  resolve_conflict_or_validation_before_acceptance: "Конфликтующие или ошибочные факты нельзя принять без уточнения.",
  leadership_source_must_belong_to_official_domain: "Страница руководства должна находиться на официальном сайте учреждения.",
  research_superseded_by_new_collection: "Есть более новый результат сбора. Откройте его для проверки.",
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
