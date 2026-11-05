"use client";

import { forwardRef, useEffect, useImperativeHandle, useState } from "react";
import { api, post, errorText, type ApiCard, type CardDetail, type ResearchRun, type Job, type Material, type ChangeSet, type ResearchHistory } from "@/lib/api";
import { type University } from "@/data/universities";
import { ApprovedCard, ResearchChanges } from "@/components/research-changes";

export type ResearchHandle = { generate: () => Promise<string>; refresh: () => Promise<string> };
type Props = { university: University; showFacts: boolean; onShowFacts: () => void; onConnected: (card: ApiCard) => void };
const fields: Record<string, string> = {
  "person.name": "ФИО", "appointment.position": "Должность", "appointment.status": "Статус назначения",
  "appointment.department": "Подразделение", "appointment.responsibilities": "Зона ответственности",
  "contact.phone": "Рабочий телефон", "contact.email": "Рабочая почта",
  "person.career": "Биография", "person.education": "Образование", "person.degree": "Учёная степень",
  "person.academic_title": "Учёное звание", "person.interests": "Научные интересы", "appointment.since": "Назначение",
  "institution.profile": "Профиль учреждения", "institution.type": "Тип учреждения", "institution.location": "Местонахождение",
  "institution.founded": "Основание", "institution.education_areas": "Направления подготовки",
  "institution.education_levels": "Уровни образования", "institution.students": "Обучающиеся", "institution.structure": "Структура и филиалы",
};
const statuses: Record<string, string> = { pending: "На проверке", verified: "Принято", rejected: "Отклонено", conflict: "Конфликт" };
const issueMessages: Record<string, string> = {
  non_person_contact_has_person_subject: "Контакт приёмной или подразделения ошибочно связан с человеком.",
  person_owner_subject_mismatch: "Имя владельца контакта не совпадает с указанным сотрудником.",
  contact_owner_scope_mismatch: "Владелец контакта не совпадает с разметкой источника.",
  contact_owner_requires_source_scope: "Источник не подтверждает принадлежность контакта.",
  contact_owner_unknown: "Владелец контакта не установлен.",
  conflicting_contact_scopes: "Источник содержит противоречивые сведения о владельце контакта.",
  person_value_not_in_same_fragment: "ФИО и значение не подтверждаются одним фрагментом источника.",
  value_not_in_fragment: "Значение отсутствует в выбранном фрагменте источника.",
  owner_not_in_fragment: "Владелец контакта отсутствует в выбранных фрагментах.",
  multiple_head_candidates: "Найдены разные кандидаты на должность руководителя.",
  subject_department_scope_mismatch: "Подразделение сотрудника не совпадает с разметкой источника.",
  conflicting_subject_department_scopes: "Выбраны сведения о сотруднике из разных подразделений; нужно уточнение.",
  student_metric_context_required: "У численности не указан период, состав контингента или единица измерения.",
  student_metric_context_not_in_scope: "Период, состав или единица показателя не подтверждаются этим источником.",
  student_metric_period_required: "Показателю численности нужен опубликованный год или дата.",
  research_interests_require_explicit_statement: "Источник не называет научные интересы явно. Перечень публикаций не подтверждает это поле.",
  institution_founding_context_required: "Нужна формулировка об основании учреждения. Отдельный год из истории недостаточен.",
  institution_structure_context_required: "Источник должен описывать структуру или подразделение. Старое название вуза недостаточно.",
  institution_location_context_required: "Нужен опубликованный город или адрес учреждения, а не регион его деятельности.",
  education_levels_require_explicit_statement: "В выбранном фрагменте не названы уровни образования; заголовка раздела недостаточно.",
};
function issueMessage(code: string) {
  return issueMessages[code] ?? "Кандидат не прошёл автоматическую проверку доказательств.";
}

function parsePdfPages(value: string): number[] | null {
  if (!value.trim()) return null;
  const pages = new Set<number>();
  for (const part of value.split(",")) {
    const match = part.trim().match(/^(\d{1,3})(?:\s*-\s*(\d{1,3}))?$/);
    if (!match) throw new Error("Страницы PDF: укажите номера или диапазоны через запятую, например 1, 3-5, 18.");
    const first = Number(match[1]), last = Number(match[2] ?? match[1]);
    if (first < 1 || last > 300 || last < first || last - first >= 20) throw new Error("Допустимо до 20 страниц PDF с номерами от 1 до 300.");
    for (let number = first; number <= last; number++) pages.add(number);
    if (pages.size > 20) throw new Error("За один сбор можно выбрать до 20 страниц PDF.");
  }
  return [...pages].sort((a, b) => a - b);
}

export const ResearchPanel = forwardRef<ResearchHandle, Props>(function ResearchPanel({ university, showFacts, onShowFacts, onConnected }, ref) {
  const [website, setWebsite] = useState(university.website ?? "");
  const [leadershipUrl, setLeadershipUrl] = useState("");
  const [biographyUrl, setBiographyUrl] = useState("");
  const [institutionUrls, setInstitutionUrls] = useState("");
  const [includeInstitution, setIncludeInstitution] = useState(true);
  const [pdfUrl, setPdfUrl] = useState("");
  const [pdfPages, setPdfPages] = useState("");
  const [card, setCard] = useState<ApiCard | null>(null);
  const [run, setRun] = useState<ResearchRun | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [reviewer, setReviewer] = useState("");
  const [reason, setReason] = useState("");
  const [sourceChecked, setSourceChecked] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [material, setMaterial] = useState<Material | null>(null);
  const [showRejected, setShowRejected] = useState(false);
  const [meetingGoal, setMeetingGoal] = useState("");
  const [meetingDate, setMeetingDate] = useState("");
  const [presentationVariant, setPresentationVariant] = useState<"compact" | "detailed">("compact");
  const [comparison, setComparison] = useState<ChangeSet | null>(null);
  const [history, setHistory] = useState<ResearchHistory[]>([]);
  const [pastChanges, setPastChanges] = useState<ChangeSet | null>(null);
  const [replacePrevious, setReplacePrevious] = useState(false);
  const busy = saving || job?.status === "queued" || job?.status === "running";
  const currentComparison = comparison?.run_id === run?.run_id && comparison?.revision === run?.revision ? comparison : null;
  const accepted = currentComparison?.approved_facts.length ?? 0;
  const retirementIds = [...new Set((currentComparison?.entries ?? []).filter((change) => change.retirement_trigger_ids.some((id) => selected.includes(id)))
    .flatMap((change) => change.retirement_ids))];
  const rejected = run?.facts.filter((fact) => fact.verification_status === "rejected").length ?? 0;
  const pending = run?.facts.filter((fact) => fact.verification_status === "pending").length ?? 0;
  const issueCounts = new Map<string, number>();
  for (const fact of run?.facts ?? []) for (const code of fact.issues) issueCounts.set(code, (issueCounts.get(code) ?? 0) + 1);
  const stageErrors = run ? [...run.errors, ...Object.values(run.results).flatMap((result) => result.errors.map((item) => item.code))] : [];
  const gaps = run ? [...new Set(Object.values(run.results).flatMap((result) => result.gaps))] : [];

  function load(detail: CardDetail) {
    setCard(detail); setWebsite(detail.website); setRun(detail.run); setJob(detail.job);
    if (detail.job?.identity?.presentation_variant) setPresentationVariant(detail.job.identity.presentation_variant);
    if (detail.job?.identity?.leadership_url) setLeadershipUrl(detail.job.identity.leadership_url);
    if (detail.job?.identity?.biography_url) setBiographyUrl(detail.job.identity.biography_url);
    if (detail.job?.identity?.institution_urls) setInstitutionUrls(detail.job.identity.institution_urls.join("\n"));
    if (detail.job?.identity?.include_institution !== undefined) setIncludeInstitution(detail.job.identity.include_institution);
    const pdf = detail.job?.identity?.pdf_sources?.[0];
    if (pdf) { setPdfUrl(pdf.url); setPdfPages(pdf.pages?.join(", ") ?? ""); }
    if (detail.job?.identity?.meeting) {
      setMeetingGoal(detail.job.identity.meeting.goal);
      setMeetingDate(detail.job.identity.meeting.date ?? "");
    }
    if (detail.job?.status === "completed" && detail.job.result?.material_id &&
        detail.job.identity?.expected_revision === detail.run?.revision) setMaterial(detail.job.result as Material);
  }

  useEffect(() => {
    let active = true;
    if (university.backendId) api<CardDetail>(`/cards/${encodeURIComponent(university.backendId)}`)
      .then((detail) => { if (active) load(detail); })
      .catch((failure) => { if (active) setError(errorText(failure)); });
    return () => { active = false; };
  }, [university.backendId]);

  useEffect(() => { setSelected([]); setSourceChecked(false); setPastChanges(null); }, [run?.run_id, run?.revision]);
  useEffect(() => { setReplacePrevious(false); }, [run?.run_id, run?.revision, selected.join("|")]);

  useEffect(() => {
    if (!run || !card) return;
    let active = true;
    Promise.all([api<ChangeSet>(`/runs/${run.run_id}/changes`), api<ResearchHistory[]>(`/cards/${encodeURIComponent(card.id)}/history`)])
      .then(([changes, runs]) => { if (active) { setComparison(changes); setHistory(runs); } })
      .catch((failure) => { if (active) setError(errorText(failure)); });
    return () => { active = false; };
  }, [run?.run_id, run?.revision, card?.id]);

  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    let active = true;
    const timer = window.setInterval(async () => {
      try {
        const next = await api<Job>(`/jobs/${job.job_id}`);
        if (!active) return;
        setJob(next);
        if (next.status === "completed" || next.status === "failed") {
          if (next.result?.run_id) setRun(await api<ResearchRun>(`/runs/${next.result.run_id}`));
          if (next.result?.material_id) setMaterial(next.result as Material);
          if (next.status === "failed") setError(next.message + (next.result?.code ? ` (${errorText(new Error(next.result.code))})` : ""));
        }
      } catch (failure) { if (active) setError(errorText(failure)); }
    }, 1400);
    return () => { active = false; window.clearInterval(timer); };
  }, [job?.job_id, job?.status]);

  async function refresh() {
    setSaving(true); setError(""); setMaterial(null);
    try {
      const pages = pdfUrl.trim() ? parsePdfPages(pdfPages) : null;
      let target = card;
      if (!target) {
        target = await post<ApiCard>("/cards", { name: university.name, city: university.city, website });
        setCard(target); onConnected(target);
      }
      const next = await post<Job>(`/cards/${encodeURIComponent(target.id)}/refresh`, {
        version: target.version, request_key: crypto.randomUUID(),
        leadership_url: leadershipUrl.trim() || null,
        biography_url: biographyUrl.trim() || null,
        institution_urls: institutionUrls.split(/\n/).map((url) => url.trim()).filter(Boolean),
        include_institution: includeInstitution,
        pdf_sources: pdfUrl.trim() ? [{url: pdfUrl.trim(), pages}] : [],
      });
      setJob(next); onShowFacts();
      return "Задание сбора создано. Состояние и результаты появятся в карточке руководства.";
    } catch (failure) { const message = errorText(failure); setError(message); return message; }
    finally { setSaving(false); }
  }

  async function review(decision: "accept" | "reject") {
    if (!run) return;
    if (!reviewer.trim() || !reason.trim()) { setError("Укажите проверяющего и основание решения."); return; }
    if (decision === "accept" && !sourceChecked) { setError("Подтвердите проверку по первоисточникам."); return; }
    if (decision === "accept" && (!currentComparison || retirementIds.length > 0 && !replacePrevious)) {
      setError("Проверьте сравнение и подтвердите замену прежних сведений."); return;
    }
    setSaving(true); setError("");
    try {
      const updated = await post<ResearchRun>(`/runs/${run.run_id}/reviews`, {
        expected_revision: run.revision, reviewer,
        decisions: selected.map((fact_id) => ({ fact_id, decision, source_checked: sourceChecked, reason })),
        retirements: decision === "accept" ? retirementIds.map((fact_id) => ({fact_id, source_checked: sourceChecked, reason})) : [],
      });
      setRun(updated); setMaterial(null);
    } catch (failure) { setError(errorText(failure)); }
    finally { setSaving(false); }
  }

  async function generate() {
    if (!run || !accepted) { const message = "Для презентации сначала примите проверенные факты о руководстве."; setError(message); onShowFacts(); return message; }
    if (busy) return "Для карточки уже выполняется задание. Дождитесь его завершения.";
    setSaving(true); setError("");
    try {
      setJob(await post<Job>(`/runs/${run.run_id}/materials`, {
        expected_revision: run.revision, request_key: crypto.randomUUID(),
        meeting: { goal: meetingGoal.trim() || "Цель встречи не задана", date: meetingDate || null },
        presentation_variant: presentationVariant,
      }));
      return "Задание формирования презентации создано. Ссылка на PPTX появится после завершения.";
    } catch (failure) { const message = errorText(failure); setError(message); return message; }
    finally { setSaving(false); }
  }
  useImperativeHandle(ref, () => ({ generate, refresh }));

  return <section className="content-card research-panel" aria-label="Актуализация и проверка фактов">
    <div className="content-card__heading"><div><h2>Руководство и сведения об учреждении</h2><span>Официальные источники → проверка → презентация</span></div></div>
    <div className="research-controls">
      <label>Официальный сайт<input type="url" value={website} disabled={Boolean(card) || Boolean(busy)}
        onChange={(event) => setWebsite(event.target.value)} placeholder="https://university.ru" aria-label="Официальный сайт" /></label>
      <button className="button button--primary" type="button" disabled={Boolean(busy) || !website.trim()} onClick={() => { void refresh(); }}>Найти и обновить руководство</button>
      <label>Вариант презентации<select value={presentationVariant} disabled={Boolean(busy)}
        onChange={(event) => setPresentationVariant(event.target.value as "compact" | "detailed")}>
        <option value="compact">Краткая карточка — 3 слайда</option>
        <option value="detailed">Подробная справка — все разделы</option>
      </select></label>
      <button className="button button--soft" type="button" disabled={Boolean(busy) || !accepted} onClick={() => { void generate(); }}>Сформировать презентацию</button>
    </div>
    {run && run.facts.some((fact) => fact.validation_status === "passed" && !fact.issues.length
      && ["pending", "verified"].includes(fact.verification_status)) && <div className="research-downloads">
      <a className="button button--soft" href={`/api/v1/ai/runs/${encodeURIComponent(run.run_id)}/preview?expected_revision=${run.revision}&presentation_variant=${presentationVariant}`}
        target="_blank" rel="noreferrer">Предпросмотр кандидатов (PPTX)</a>
      <span>Для проверки содержания и вёрстки. Этот файл не означает принятие фактов.</span>
    </div>}
    <small>{presentationVariant === "compact" ? "Краткая карточка: руководитель, учреждение, повестка встречи. Полные значения и цитаты сохраняются в справке и заметках." : "Подробная справка: биография, руководство, показатели и отдельные слайды источников; объём зависит от данных."}</small>
    <details className="research-source-options"><summary>Источники руководства, биографии и справки об учреждении</summary>
      <label>Страница руководства<input type="url" value={leadershipUrl} disabled={Boolean(busy)}
        onChange={(event) => setLeadershipUrl(event.target.value)} placeholder="https://university.ru/sveden/managers/" /></label>
      <label>Официальная биография первого руководителя<input type="url" value={biographyUrl} disabled={Boolean(busy)}
        onChange={(event) => setBiographyUrl(event.target.value)} placeholder="https://university.ru/about/rector/" /></label>
      <label className="research-ack"><input type="checkbox" checked={includeInstitution} disabled={Boolean(busy)}
        onChange={(event) => setIncludeInstitution(event.target.checked)} />Также собрать справку об учреждении</label>
      <label>Страницы направлений, численности и структуры (до 3 ссылок, по одной в строке)<textarea value={institutionUrls} disabled={Boolean(busy)}
        onChange={(event) => setInstitutionUrls(event.target.value)} rows={3} placeholder="https://university.ru/about/" /></label>
      <label>Официальный отчёт в PDF<input type="url" value={pdfUrl} disabled={Boolean(busy)}
        onChange={(event) => setPdfUrl(event.target.value)} placeholder="https://university.ru/report.pdf" /></label>
      <label>Страницы PDF (номера с 1, до 20 страниц)<input value={pdfPages} disabled={Boolean(busy)}
        onChange={(event) => setPdfPages(event.target.value)} placeholder="1, 3-5, 18" /></label>
      <p>Без номеров обрабатываются первые 20 страниц PDF. Используются текстовые PDF и HTML официального сайта. Для численности нужны период и состав контингента; сканы пока не распознаются. Новые сведения потребуется проверить.</p>
    </details>
    <fieldset className="research-meeting" disabled={Boolean(busy)}>
      <legend>Контекст встречи</legend>
      <div className="research-controls">
        <label>Цель встречи<input value={meetingGoal} onChange={(event) => setMeetingGoal(event.target.value)}
          maxLength={1000} placeholder="Например: обсудить подготовку ИТ-специалистов" /></label>
        <label>Дата встречи (необязательно)<input type="date" value={meetingDate} onChange={(event) => setMeetingDate(event.target.value)} /></label>
      </div>
      <small>Параметры используются при следующем формировании и сохраняются с материалом. Цель встречи не считается фактом о вузе.</small>
    </fieldset>
    {job && <div className="research-progress" role="status"><span>{job.message}</span><progress value={job.progress} max={100} /></div>}
    {error && <p className="research-error" role="alert">{error}</p>}
    {material && <div className="research-downloads"><a className="button button--primary" href={material.download_url}>Скачать PPTX</a><a className="button button--soft" href={material.brief_url} target="_blank" rel="noreferrer">Открыть справку</a><span>{material.presentation_variant === "compact" ? "Краткая карточка · 3 слайда" : "Подробная справка"} · черновик для проверки перед встречей</span></div>}
    {run && <p className="research-summary">В последнем сборе: {run.facts.length} кандидатов · на проверке: {pending} · в подтверждённой карточке: {accepted} · отклонено: {rejected} · {run.status === "partial" ? "сбор частичный" : run.status === "failed" ? "сбор не выполнен; прежние принятые сведения сохранены" : "результат сохранён"}.
      {!showFacts && <button className="text-button" type="button" onClick={onShowFacts}>Проверить факты</button>}</p>}
    {run && Object.values(run.results).some((result) => result.run?.provider === "fixture") &&
      <p className="research-summary">Учебный прогон: сохранённые ответы модели, без обращения к GigaChat.</p>}
    {run && Object.values(run.results).some((result) => result.run?.provider === "gigachat-recording") &&
      <p className="research-summary">Сохранённый реальный ответ GigaChat: повторная проверка источника и разметки без нового запроса к модели.</p>}
    {run && Object.values(run.results).some((result) => result.run?.provider === "prepared-candidates") &&
      <p className="research-summary">Добавлены кандидаты из сохранённых источников и ответов GigaChat. Часть выдержек уточнена для проверки; новые сведения ещё не приняты сотрудником.</p>}
    {run && (issueCounts.size > 0 || stageErrors.length > 0 || run.parser_errors.length > 0 || (run.parser_warnings?.length ?? 0) > 0 || gaps.length > 0) &&
      <details className="research-diagnostics"><summary>Качество сбора: ошибки, ограничения и пропуски</summary>
        <p>Автоматическая проверка не подтверждает полноту сведений. Сверьте нужные факты по первоисточникам.</p>
        {issueCounts.size > 0 && <ul>{[...issueCounts].map(([code, count]) => <li key={code}>{issueMessage(code)} Кандидатов: {count}.</li>)}</ul>}
        {stageErrors.length > 0 && <ul>{[...new Set(stageErrors)].map((code) => <li key={code}>{errorText(new Error(code.replace(/^0\.[23]:/, "")))}</li>)}</ul>}
        {run.parser_errors.length > 0 && <><p>Не удалось получить часть источников: {run.parser_errors.length}.</p><ul>{run.parser_errors.filter((code) => code.startsWith("pdf_") || code.includes("pdf_")).map((code, index) => <li key={index}>{errorText(new Error(code))}</li>)}</ul></>}
        {run.parser_warnings?.some((warning) => warning.startsWith("PDF:")) && <ul>{run.parser_warnings.filter((warning) => warning.startsWith("PDF:")).map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
        {(run.parser_warnings?.length ?? 0) > 0 && <p>Парсер сообщил об ограничениях: {run.parser_warnings.length}. Проверьте, все ли нужные страницы обработаны.</p>}
        {gaps.length > 0 && <><p>Пропуски, отмеченные моделью (не доказывают отсутствие сведений):</p><ul>{gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul></>}
      </details>}
    {showFacts && run && <>
      {currentComparison && <><ApprovedCard facts={currentComparison.approved_facts} labels={fields}/>
        <ResearchChanges data={currentComparison} labels={fields} selected={selected} onSelect={setSelected} busy={Boolean(busy)}/></>}
      {history.length > 0 && <details className="research-history"><summary>История сборов ({history.length})</summary>
        {history.map((item) => <p key={item.run_id}>{new Date(item.created_at).toLocaleString("ru-RU")} · принятых сведений: {item.approved_count}
          {item.run_id === run.run_id ? " · текущий сбор" : <button className="text-button" type="button" onClick={() => {
            void api<ChangeSet>(`/runs/${item.run_id}/changes`).then(setPastChanges).catch((failure) => setError(errorText(failure)));
          }}>Просмотреть</button>}</p>)}
        {pastChanges && <><p>Архивный сбор: только просмотр.</p><ResearchChanges data={pastChanges} labels={fields}/>
          <button className="text-button" type="button" onClick={() => setPastChanges(null)}>Закрыть архивный сбор</button></>}
      </details>}
      <button className="text-button" type="button" disabled={Boolean(busy)} onClick={() => setSelected(run.facts
        .filter((fact) => fact.verification_status === "pending" && fact.validation_status === "passed" && !fact.issues.length)
        .map((fact) => fact.fact_id))}>Выбрать все факты без ошибок</button>
      <button className="text-button" type="button" disabled={Boolean(busy)} onClick={() => setSelected(run.facts
        .filter((fact) => fact.verification_status === "pending" && fact.validation_status === "passed" && !fact.issues.length
          && fact.subject.person_name && !fact.subject.department)
        .map((fact) => fact.fact_id))}>Выбрать сотрудников без привязки к подразделению</button>
      {rejected > 0 && <button className="text-button" type="button" onClick={() => setShowRejected((value) => !value)}>
        {showRejected ? "Скрыть" : "Показать"} автоматически отклонённые факты ({rejected})</button>}
      <div className="research-facts">{run.facts.filter((fact) => showRejected || fact.verification_status !== "rejected").map((fact) => <article className="research-fact" key={fact.fact_id}>
        <label className="research-fact__selection"><input type="checkbox" checked={selected.includes(fact.fact_id)} disabled={Boolean(busy)}
          onChange={(event) => setSelected((current) => event.target.checked ? [...current, fact.fact_id] : current.filter((id) => id !== fact.fact_id))} />
          <span><strong>{fact.subject.person_name ?? fact.subject.entity_name ?? fact.contact_owner?.label ?? "Контакт организации"}</strong><span>{fields[fact.field] ?? fact.field}: {fact.value}</span></span>
          <span className={`research-status research-status--${fact.verification_status}`}>{statuses[fact.verification_status]}</span>
        </label>
        {fact.subject.department && <p>Подразделение: {fact.subject.department}</p>}
        {fact.metric_context && <p>Период: {fact.metric_context.period} · Состав: {fact.metric_context.scope} · Единица: {fact.metric_context.unit}{fact.metric_context.coverage ? ` · Охват: ${fact.metric_context.coverage}` : ""}</p>}
        {fact.origin === "parser_projection" && <small>Кандидат из структурированных данных источника; требуется проверка сотрудником.</small>}
        {fact.origin === "curated_candidate" && <small>Кандидат уточнён по источнику при подготовке предпросмотра; требуется проверка сотрудником.</small>}
        {fact.issues.length > 0 && <div className="research-error">{fact.issues.map((code) => <p key={code}>{issueMessage(code)}</p>)}Принятие заблокировано до получения корректного кандидата.</div>}
        <details><summary>Источники и подтверждающий текст ({fact.evidence.length})</summary>{fact.evidence.map((evidence, index) => <div className="research-evidence" key={index}>
          <a href={evidence.page_number ? `${evidence.url.split("#")[0]}#page=${evidence.page_number}` : evidence.url} target="_blank" rel="noreferrer">{evidence.page_number ? `PDF · страница ${evidence.page_number} · ` : ""}{evidence.url}</a><small>Получено: {new Date(evidence.retrieved_at).toLocaleString("ru-RU")}</small><blockquote>{evidence.quote}</blockquote>
        </div>)}</details>
      </article>)}</div>
      <div className="research-review">
        <label>Проверяющий<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} placeholder="Имя и фамилия" /></label>
        <label>Основание решения<input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Что сверено или почему отклонено" /></label>
        <label className="research-ack"><input type="checkbox" checked={sourceChecked} onChange={(event) => setSourceChecked(event.target.checked)} />Я проверил(а) выбранные факты по действующим первоисточникам</label>
        {retirementIds.length > 0 && <label className="research-ack research-replacement"><input type="checkbox" checked={replacePrevious}
          onChange={(event) => setReplacePrevious(event.target.checked)} />Подтверждаю замену: прежних сведений будет архивировано {retirementIds.length}. Их значения и решения останутся в истории.</label>}
        <div className="research-controls"><button className="button button--primary" type="button" disabled={Boolean(busy) || !selected.length} onClick={() => { void review("accept"); }}>Принять выбранные ({selected.length})</button>
        <button className="button button--soft" type="button" disabled={Boolean(busy) || !selected.length} onClick={() => { void review("reject"); }}>Отклонить выбранные</button></div>
      </div>
    </>}
  </section>;
});
