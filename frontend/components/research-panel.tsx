"use client";

import { forwardRef, useEffect, useImperativeHandle, useState } from "react";
import { api, post, errorText, type ApiCard, type CardDetail, type ResearchRun, type Job, type Material } from "@/lib/api";
import { Icon } from "@/components/icon";
import { type University } from "@/data/universities";

export type ResearchHandle = { generate: () => Promise<string>; refresh: () => Promise<string> };
type Props = { university: University; showFacts: boolean; onShowFacts: () => void; onConnected: (card: ApiCard) => void };
const fields: Record<string, string> = {
  "person.name": "ФИО", "appointment.position": "Должность", "appointment.status": "Статус назначения",
  "appointment.department": "Подразделение", "appointment.responsibilities": "Зона ответственности",
  "contact.phone": "Рабочий телефон", "contact.email": "Рабочая почта",
  "person.career": "Биография", "person.education": "Образование", "person.degree": "Учёная степень",
};
const statuses: Record<string, string> = { pending: "На проверке", verified: "Принято", rejected: "Отклонено", conflict: "Конфликт" };

export const ResearchPanel = forwardRef<ResearchHandle, Props>(function ResearchPanel({ university, showFacts, onShowFacts, onConnected }, ref) {
  const [website, setWebsite] = useState(university.website ?? "");
  const [leadershipUrl, setLeadershipUrl] = useState("");
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
  const [downloadFormat, setDownloadFormat] = useState<"pptx" | "docx">("pptx");
  const [preview, setPreview] = useState("");
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [showRejected, setShowRejected] = useState(false);
  const busy = saving || job?.status === "queued" || job?.status === "running";
  const accepted = run?.facts.filter((fact) => fact.verification_status === "verified").length ?? 0;
  const rejected = run?.facts.filter((fact) => fact.verification_status === "rejected").length ?? 0;

  function load(detail: CardDetail) {
    setCard(detail); setWebsite(detail.website); setRun(detail.run); setJob(detail.job);
    if (detail.job?.identity?.leadership_url) setLeadershipUrl(detail.job.identity.leadership_url);
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

  useEffect(() => { setSelected([]); setSourceChecked(false); }, [run?.revision]);

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
      let target = card;
      if (!target) {
        target = await post<ApiCard>("/cards", { name: university.name, city: university.city, website });
        setCard(target); onConnected(target);
      }
      const next = await post<Job>(`/cards/${encodeURIComponent(target.id)}/refresh`, {
        version: target.version, request_key: crypto.randomUUID(),
        leadership_url: leadershipUrl.trim() || null,
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
    setSaving(true); setError("");
    try {
      const updated = await post<ResearchRun>(`/runs/${run.run_id}/reviews`, {
        expected_revision: run.revision, reviewer,
        decisions: selected.map((fact_id) => ({ fact_id, decision, source_checked: sourceChecked, reason })),
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
        meeting: { goal: "Встреча с руководством образовательной организации" },
      }));
      return "Задание формирования презентации создано. Ссылка на PPTX появится после завершения.";
    } catch (failure) { const message = errorText(failure); setError(message); return message; }
    finally { setSaving(false); }
  }

  async function openPreview() {
    if (!material) return;
    setPreviewLoading(true); setError("");
    try {
      const response = await fetch(material.brief_url, { cache: "no-store" });
      if (!response.ok) throw new Error("preview_unavailable");
      setPreview(await response.text());
      setPreviewOpen(true);
    } catch (failure) {
      setError(failure instanceof Error && failure.message !== "preview_unavailable"
        ? "Не удалось загрузить предварительный просмотр отчёта." : "Предварительный просмотр отчёта недоступен.");
    } finally { setPreviewLoading(false); }
  }

  const downloadUrl = material && `${material.download_url}?format=${downloadFormat}`;
  useImperativeHandle(ref, () => ({ generate, refresh }));

  return <section className="content-card research-panel" aria-label="Актуализация и проверка фактов">
    <div className="content-card__heading"><div><h2>Сведения о руководстве</h2><span>Официальные источники → проверка → презентация</span></div></div>
    <div className="research-controls">
      <label>Официальный сайт<input type="url" value={website} disabled={Boolean(card) || Boolean(busy)}
        onChange={(event) => setWebsite(event.target.value)} placeholder="https://university.ru" aria-label="Официальный сайт" /></label>
      <button className="button button--primary" type="button" disabled={Boolean(busy) || !website.trim()} onClick={() => { void refresh(); }}>Найти и обновить руководство</button>
      <button className="button button--soft" type="button" disabled={Boolean(busy) || !accepted} onClick={() => { void generate(); }}>Сформировать презентацию</button>
    </div>
    <details className="research-source-options"><summary>Указать страницу руководства, если она известна</summary>
      <label>Страница руководства<input type="url" value={leadershipUrl} disabled={Boolean(busy)}
        onChange={(event) => setLeadershipUrl(event.target.value)} placeholder="https://university.ru/sveden/managers/" /></label>
    </details>
    {job && <div className="research-progress" role="status"><span>{job.message}</span><progress value={job.progress} max={100} /></div>}
    {error && <p className="research-error" role="alert">{error}</p>}
    {material && <div className="research-downloads">
      <button className="button button--soft" type="button" disabled={previewLoading} onClick={() => { void openPreview(); }}>
        {previewLoading ? "Загружаем…" : "Просмотреть отчёт"}
      </button>
      <label className="research-format-select">Формат файла
        <select value={downloadFormat} onChange={(event) => setDownloadFormat(event.target.value as "pptx" | "docx")}>
          <option value="pptx">PowerPoint (.pptx)</option>
          <option value="docx">Word (.docx)</option>
        </select>
      </label>
      <a className="button button--primary" href={downloadUrl ?? undefined}>
        <span>Скачать {downloadFormat.toUpperCase()}</span>
      </a>
      <span>Черновик для проверки перед встречей</span>
    </div>}
    {run && <p className="research-summary">Найдено фактов: {run.facts.length} · принято: {accepted} · {run.status === "partial" ? "сбор частичный" : "результат сохранён"}.
      {!showFacts && <button className="text-button" type="button" onClick={onShowFacts}>Проверить факты</button>}</p>}
    {run && Object.values(run.results).some((result) => result.run?.provider === "fixture") &&
      <p className="research-summary">Учебный прогон: сохранённые ответы модели, без обращения к GigaChat.</p>}
    {showFacts && run && <>
      <button className="text-button" type="button" disabled={Boolean(busy)} onClick={() => setSelected(run.facts
        .filter((fact) => fact.verification_status === "pending" && fact.validation_status === "passed" && !fact.issues.length)
        .map((fact) => fact.fact_id))}>Выбрать все факты без ошибок</button>
      {rejected > 0 && <button className="text-button" type="button" onClick={() => setShowRejected((value) => !value)}>
        {showRejected ? "Скрыть" : "Показать"} автоматически отклонённые факты ({rejected})</button>}
      <div className="research-facts">{run.facts.filter((fact) => showRejected || fact.verification_status !== "rejected").map((fact) => <article className="research-fact" key={fact.fact_id}>
        <label className="research-fact__selection"><input type="checkbox" checked={selected.includes(fact.fact_id)} disabled={Boolean(busy)}
          onChange={(event) => setSelected((current) => event.target.checked ? [...current, fact.fact_id] : current.filter((id) => id !== fact.fact_id))} />
          <span><strong>{fact.subject.person_name ?? fact.contact_owner?.label ?? "Контакт организации"}</strong><span>{fields[fact.field] ?? fact.field}: {fact.value}</span></span>
          <span className={`research-status research-status--${fact.verification_status}`}>{statuses[fact.verification_status]}</span>
        </label>
        {fact.issues.length > 0 && <p className="research-error">Нужна дополнительная проверка связи и значения по источнику. Автоматическое принятие недоступно.</p>}
        <details><summary>Источники и подтверждающий текст ({fact.evidence.length})</summary>{fact.evidence.map((evidence, index) => <div className="research-evidence" key={index}>
          <a href={evidence.url} target="_blank" rel="noreferrer">{evidence.url}</a><small>Получено: {new Date(evidence.retrieved_at).toLocaleString("ru-RU")}</small><blockquote>{evidence.quote}</blockquote>
        </div>)}</details>
      </article>)}</div>
      <div className="research-review">
        <label>Проверяющий<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} placeholder="Имя и фамилия" /></label>
        <label>Основание решения<input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Что сверено или почему отклонено" /></label>
        <label className="research-ack"><input type="checkbox" checked={sourceChecked} onChange={(event) => setSourceChecked(event.target.checked)} />Я проверил(а) выбранные факты по действующим первоисточникам</label>
        <div className="research-controls"><button className="button button--primary" type="button" disabled={Boolean(busy) || !selected.length} onClick={() => { void review("accept"); }}>Принять выбранные ({selected.length})</button>
        <button className="button button--soft" type="button" disabled={Boolean(busy) || !selected.length} onClick={() => { void review("reject"); }}>Отклонить выбранные</button></div>
      </div>
    </>}
    {previewOpen && <div className="report-preview-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setPreviewOpen(false); }}>
      <section className="report-preview" role="dialog" aria-modal="true" aria-labelledby="report-preview-title">
        <header className="report-preview__header">
          <div><span className="eyebrow">ПРЕДВАРИТЕЛЬНЫЙ ПРОСМОТР</span><h2 id="report-preview-title">Отчёт к встрече: {university.name}</h2></div>
          <button className="icon-button" type="button" onClick={() => setPreviewOpen(false)} aria-label="Закрыть предпросмотр"><Icon name="close" size={19} /></button>
        </header>
        <div className="report-preview__content">
          {preview.split("\n").map((line, index) => {
            if (line.startsWith("# ")) return <h3 key={index}>{line.slice(2)}</h3>;
            if (line.startsWith("## ")) return <h4 key={index}>{line.slice(3)}</h4>;
            if (line.startsWith("### ")) return <h5 key={index}>{line.slice(4)}</h5>;
            if (line.startsWith("- ")) return <p className="report-preview__bullet" key={index}>{line.slice(2)}</p>;
            if (!line.trim()) return <div className="report-preview__space" key={index} />;
            return <p key={index}>{line.replaceAll("**", "")}</p>;
          })}
        </div>
      </section>
    </div>}
  </section>;
});

