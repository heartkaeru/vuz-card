"use client";

import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, post, errorText, downloadFile, type ApiCard, type CardDetail, type Fact, type ResearchRun, type Job, type Material, type ChangeSet, type PptxPreviewData, type ResearchHistory, type SavedMaterial, type PartnershipMatch, type CardActionResult } from "@/lib/api";
import { Icon } from "@/components/icon";
import { PptxPreview } from "@/components/pptx-preview";
import { type University } from "@/data/universities";
import { ResearchChanges } from "@/components/research-changes";
import { buildPersonAliases, personIdentityKey } from "@/lib/person-identity";

export type ResearchHandle = { generate: () => Promise<CardActionResult>; refresh: () => Promise<CardActionResult>; openReportOptions: () => void; showRecheck: () => void };
type Props = {
  university: University; showFacts: boolean; reviewOutlet: HTMLDivElement | null; onShowFacts: () => void;
  onConnected: (card: ApiCard) => void; onRunChange: (run: ResearchRun | null) => void;
};
type ReviewTab = "candidates" | "conflicts" | "accepted";
const fields: Record<string, string> = {
  "person.name": "ФИО", "appointment.position": "Должность", "appointment.status": "Статус назначения",
  "appointment.department": "Подразделение", "appointment.responsibilities": "Зона ответственности",
  "contact.phone": "Рабочий телефон", "contact.email": "Рабочая почта",
  "person.career": "Биография", "person.education": "Образование", "person.degree": "Учёная степень",
  "person.academic_title": "Учёное звание", "person.interests": "Научные интересы", "appointment.since": "Назначение",
  "institution.profile": "Профиль учреждения", "institution.type": "Тип учреждения", "institution.location": "Местонахождение",
  "institution.founded": "Основание", "institution.education_areas": "Направления подготовки",
  "institution.education_levels": "Уровни образования", "institution.students": "Обучающиеся", "institution.structure": "Структура и филиалы",
  "project.name": "Название проекта", "project.program": "Программа", "project.role": "Роль учреждения",
  "project.status": "Статус проекта", "project.period": "Период проекта", "project.description": "Описание проекта",
  "event.name": "Название события", "event.type": "Вид события", "event.date": "Дата события",
  "event.status": "Статус события", "event.description": "Описание события",
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

type FactGroup = { key: string; title: string; description: string; facts: Fact[] };

function normalizeGroupPart(value: string) {
  return value.normalize("NFKC").toLocaleLowerCase("ru-RU").replace(/ё/g, "е").replace(/\s+/g, " ").trim();
}

function groupReviewFacts(facts: Fact[], institutionName: string, identityFacts = facts): FactGroup[] {
  const groups = new Map<string, FactGroup>();
  const aliases = buildPersonAliases(identityFacts.flatMap((fact) => {
    const name = fact.subject.person_name ?? (fact.contact_owner?.kind === "person" ? fact.contact_owner.label : null);
    return name ? [{ name, department: fact.subject.department }] : [];
  }));
  for (const fact of facts) {
    const personName = (fact.subject.person_name ?? (fact.contact_owner?.kind === "person" ? fact.contact_owner.label : null))?.trim();
    const entityName = fact.subject.entity_name?.trim();
    let key: string;
    let title: string;
    let description: string;

    if (personName) {
      const department = fact.subject.department?.trim() ?? "";
      key = `person:${personIdentityKey(personName, department, aliases)}`;
      title = personName;
      description = department ? `Сведения о сотруднике · ${department}` : "Сведения о сотруднике";
    } else if (fact.subject.entity_id && entityName) {
      key = `entity:${fact.subject.entity_kind ?? "entity"}:${fact.subject.entity_id}`;
      title = entityName;
      description = fact.subject.entity_kind === "project" ? "Сведения о проекте"
        : fact.subject.entity_kind === "event" ? "Сведения о мероприятии" : "Сведения об учреждении";
    } else if (fact.field.startsWith("institution.")) {
      key = `institution:${normalizeGroupPart(entityName ?? institutionName)}`;
      title = entityName ?? institutionName;
      description = "Сведения об учреждении";
    } else if (fact.contact_owner && fact.contact_owner.kind !== "unknown") {
      key = `contact:${fact.contact_owner.kind}:${normalizeGroupPart(fact.contact_owner.label)}`;
      title = fact.contact_owner.label;
      description = "Контактные сведения владельца";
    } else {
      key = `fact:${fact.fact_id}`;
      title = fact.contact_owner?.label ?? "Контакт организации";
      description = fields[fact.field] ?? fact.field;
    }

    const group = groups.get(key);
    if (group) {
      group.facts.push(fact);
      if (personName && personName.trim().split(/\s+/).length > group.title.split(/\s+/).length) {
        group.title = personName;
      }
    }
    else groups.set(key, { key, title, description, facts: [fact] });
  }
  return [...groups.values()];
}

export const ResearchPanel = forwardRef<ResearchHandle, Props>(function ResearchPanel({ university, showFacts, reviewOutlet, onShowFacts, onConnected, onRunChange }, ref) {
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
  const [downloadFormat, setDownloadFormat] = useState<"pptx" | "docx">("pptx");
  const [formatPickerOpen, setFormatPickerOpen] = useState(false);
  const [briefPreview, setBriefPreview] = useState("");
  const [presentationPreview, setPresentationPreview] = useState<PptxPreviewData | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [downloadingPresentation, setDownloadingPresentation] = useState(false);
  const [savingToReports, setSavingToReports] = useState(false);
  const [savedMaterialId, setSavedMaterialId] = useState<string | null>(null);
  const [previewActionError, setPreviewActionError] = useState("");
  const [previewAfterGeneration, setPreviewAfterGeneration] = useState(false);
  const [previewAfterGenerationFrom, setPreviewAfterGenerationFrom] = useState<string | null>(null);
  const [showRejected, setShowRejected] = useState(false);
  const [meetingGoal, setMeetingGoal] = useState("");
  const [meetingDate, setMeetingDate] = useState("");
  const [partnership, setPartnership] = useState<{ key: string; matches: PartnershipMatch[] } | null>(null);
  const [matching, setMatching] = useState(false);
  const [includePartnership, setIncludePartnership] = useState(false);
  const [presentationVariant, setPresentationVariant] = useState<"compact" | "detailed">("compact");
  const [comparison, setComparison] = useState<ChangeSet | null>(null);
  const [history, setHistory] = useState<ResearchHistory[]>([]);
  const [pastChanges, setPastChanges] = useState<ChangeSet | null>(null);
  const [reviewTab, setReviewTab] = useState<ReviewTab>("candidates");
  const [replacePrevious, setReplacePrevious] = useState(false);
  const [recheckConfirmed, setRecheckConfirmed] = useState(false);
  const recheckRef = useRef<HTMLDetailsElement>(null);
  const busy = saving || matching || job?.status === "queued" || job?.status === "running";
  const websiteSuggestionCodes = new Set(["no_valid_extraction", "no_source_documents", "empty_source_documents", "collection_failed"]);
  const suggestWebsite = !website.trim() || job?.status === "failed" && websiteSuggestionCodes.has(job.result?.code ?? "");
  const currentComparison = comparison?.run_id === run?.run_id && comparison?.revision === run?.revision ? comparison : null;
  const accepted = currentComparison?.approved_facts.length
    ?? run?.facts.filter((fact) => fact.verification_status === "verified" && fact.validation_status === "passed" && !fact.issues.length).length
    ?? 0;
  const recheckIds = new Set(currentComparison?.recheck_fact_ids);
  const factsToRecheck = currentComparison?.approved_facts.filter((fact) => recheckIds.has(fact.fact_id)) ?? [];
  const meeting = { goal: meetingGoal.trim() || "Цель встречи не задана", date: meetingDate || null };
  const partnershipKey = JSON.stringify([run?.run_id, run?.revision, meeting]);
  const partnershipMatches = partnership?.key === partnershipKey ? partnership.matches : null;
  const downloadUrl = material && `${material.download_url}${material.download_url.includes("?") ? "&" : "?"}format=${downloadFormat}`;
  const retirementIds = [...new Set((currentComparison?.entries ?? []).filter((change) => change.retirement_trigger_ids.some((id) => selected.includes(id)))
    .flatMap((change) => change.retirement_ids))];
  const reconfirmedIds = new Set(Object.values(run?.reconfirmed_facts ?? {}).flat());
  const candidates = run?.facts.filter((fact) => fact.origin !== "manual_edit" && !run.retired_fact_ids?.includes(fact.fact_id)
    && !reconfirmedIds.has(fact.fact_id)) ?? [];
  const canAccept = (fact: Fact) => fact.verification_status === "pending" && fact.validation_status === "passed" && !fact.issues.length;
  const readyForReview = candidates.filter(canAccept);
  const problematic = candidates.filter((fact) => fact.verification_status === "rejected"
    || fact.verification_status === "conflict" || fact.validation_status === "failed" || fact.issues.length > 0);
  const blockedSelection = problematic.some((fact) => selected.includes(fact.fact_id));
  const rejected = run?.facts.filter((fact) => fact.verification_status === "rejected").length ?? 0;
  const reconfirmed = Object.keys(run?.reconfirmed_facts ?? {}).length;
  const issueCounts = new Map<string, number>();
  for (const fact of run?.facts ?? []) for (const code of fact.issues) issueCounts.set(code, (issueCounts.get(code) ?? 0) + 1);
  const stageErrors = run ? [...run.errors, ...Object.values(run.results).flatMap((result) => result.errors.map((item) => item.code))] : [];
  const gaps = run ? [...new Set(Object.values(run.results).flatMap((result) => result.gaps))] : [];
  const conflictCount = currentComparison?.entries.filter((change) => change.kind === "changed" || change.kind === "head_change").length ?? 0;
  const acceptedFacts = currentComparison?.approved_facts
    ?? run?.facts.filter((fact) => fact.verification_status === "verified" && fact.validation_status === "passed" && !fact.issues.length)
    ?? [];
  const acceptedFactIds = new Set(acceptedFacts.map((fact) => fact.fact_id));
  const selectedAcceptedCount = selected.filter((id) => acceptedFactIds.has(id)).length;
  const candidateFactIds = new Set((run?.facts ?? []).filter((fact) => fact.verification_status !== "verified").map((fact) => fact.fact_id));
  const selectedCandidateCount = selected.filter((id) => candidateFactIds.has(id)).length;
  const identityFacts = [...(run?.facts ?? []), ...acceptedFacts];
  const pending = candidates.filter((fact) => fact.verification_status === "pending").length;
  const visibleFactGroups = run ? groupReviewFacts(
    candidates.filter((fact) => fact.verification_status !== "verified" && (showRejected || fact.verification_status !== "rejected")),
    university.name, identityFacts,
  ) : [];
  const acceptedFactGroups = acceptedFacts.length ? groupReviewFacts(acceptedFacts, university.name, identityFacts) : [];

  function load(detail: CardDetail) {
    setCard(detail); setWebsite(detail.website); setRun(detail.run); onRunChange(detail.run); setJob(detail.job);
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
    if (detail.job?.status === "failed" && detail.job.result?.code === "facts_require_freshness_check"
        && detail.job.identity?.expected_revision !== detail.run?.revision) setJob(null);
    if (detail.job?.status === "completed" && detail.job.result?.material_id &&
        detail.job.identity?.expected_revision === detail.run?.revision) setMaterial(detail.job.result as Material);
  }

  function showCandidates() {
    setReviewTab("candidates");
    onShowFacts();
  }

  useEffect(() => {
    let active = true;
    if (university.backendId) api<CardDetail>(`/cards/${encodeURIComponent(university.backendId)}`)
      .then((detail) => { if (active) load(detail); })
      .catch((failure) => { if (active) setError(errorText(failure)); });
    return () => { active = false; };
  }, [university.backendId]);

  useEffect(() => { setSelected([]); setPastChanges(null); setRecheckConfirmed(false); setReviewTab("candidates"); }, [run?.run_id, run?.revision]);
  useEffect(() => { setSourceChecked(false); setReplacePrevious(false); }, [run?.run_id, run?.revision, selected.join("|")]);

  useEffect(() => {
    if (!run || !card) return;
    let active = true;
    api<ChangeSet>(`/runs/${run.run_id}/changes`)
      .then((changes) => { if (active) setComparison(changes); })
      .catch((failure) => { if (active) setError(errorText(failure)); });
    api<ResearchHistory[]>(`/cards/${encodeURIComponent(card.id)}/history`)
      .then((runs) => { if (active) setHistory(runs); })
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
          if (next.result?.run_id) {
            const updatedRun = await api<ResearchRun>(`/runs/${next.result.run_id}`);
            setRun(updatedRun); onRunChange(updatedRun);
          }
          if (next.result?.material_id) setMaterial(next.result as Material);
          if (next.status === "failed") {
            setPreviewAfterGeneration(false);
            setPreviewAfterGenerationFrom(null);
            setError(next.message + (next.result?.code ? ` (${errorText(new Error(next.result.code))})` : ""));
          }
        }
      } catch (failure) { if (active) setError(errorText(failure)); }
    }, 1400);
    return () => { active = false; window.clearInterval(timer); };
  }, [job?.job_id, job?.status]);

  async function refresh(): Promise<CardActionResult> {
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
      setRun(null); onRunChange(null); setJob(next); showCandidates();
      return { message: "Задание сбора создано. Состояние и результаты появятся в карточке руководства.", queued: true };
    } catch (failure) { const message = errorText(failure); setError(message); return { message }; }
    finally { setSaving(false); }
  }

  async function review(decision: "accept" | "reject" | "revoke") {
    if (!run) return;
    const decisionIds = decision === "revoke" ? selected.filter((id) => acceptedFactIds.has(id))
      : selected.filter((id) => candidateFactIds.has(id));
    if (!decisionIds.length) { setError(decision === "revoke" ? "Выберите принятые сведения для отмены." : "Выберите сведения для решения."); return; }
    if (!reviewer.trim() || !reason.trim()) { setError("Укажите проверяющего и основание решения."); return; }
    if (decision === "accept" && !sourceChecked) { setError("Подтвердите проверку по первоисточникам."); return; }
    if (decision === "accept" && (!currentComparison || retirementIds.length > 0 && !replacePrevious)) {
      setError("Проверьте сравнение и подтвердите замену прежних сведений."); return;
    }
    setSaving(true); setError("");
    try {
      const updated = await post<ResearchRun>(`/runs/${run.run_id}/reviews`, {
        expected_revision: run.revision, reviewer,
        decisions: decisionIds.map((fact_id) => ({ fact_id, decision, source_checked: decision === "revoke" ? false : sourceChecked, reason })),
        retirements: decision === "accept" ? retirementIds.map((fact_id) => ({fact_id, source_checked: sourceChecked, reason})) : [],
      });
      setRun(updated); onRunChange(updated); setMaterial(null);
    } catch (failure) { setError(errorText(failure)); }
    finally { setSaving(false); }
  }

  async function recommendPartnership() {
    if (!run || !accepted || busy) return;
    setMatching(true); setError("");
    try {
      const matches = await post<PartnershipMatch[]>(`/runs/${run.run_id}/partnership-matches`, {
        expected_revision: run.revision, meeting,
      });
      setPartnership({ key: partnershipKey, matches });
      setIncludePartnership(false);
      setMaterial(null);
    } catch (failure) { setError(errorText(failure)); }
    finally { setMatching(false); }
  }

  async function recheck() {
    if (!run || !factsToRecheck.length || !recheckConfirmed || !reviewer.trim() || busy) return;
    setSaving(true); setError("");
    try {
      const updated = await post<ResearchRun>(`/runs/${run.run_id}/recheck`, {
        expected_revision: run.revision, fact_ids: factsToRecheck.map((fact) => fact.fact_id),
        reviewer: reviewer.trim(), source_checked: true,
      });
      setRun(updated); onRunChange(updated); setMaterial(null);
      if (job?.status === "failed" && job.result?.code === "facts_require_freshness_check") setJob(null);
    } catch (failure) { setError(errorText(failure)); }
    finally { setSaving(false); }
  }

  async function generate(): Promise<CardActionResult> {
    if (busy) return { message: "Для карточки уже выполняется задание. Дождитесь его завершения." };
    if (!run || (!accepted && !material)) {
      const message = "Для презентации сначала примите проверенные факты о руководстве.";
      setError(message); showCandidates(); return { message };
    }
    if (factsToRecheck.length) {
      const message = errorText(new Error("facts_require_freshness_check"));
      setError(message);
      return { message, needsRecheck: true };
    }
    setSaving(true); setError("");
    try {
      setJob(await post<Job>(`/runs/${run.run_id}/materials`, {
        expected_revision: run.revision, request_key: crypto.randomUUID(),
        meeting,
        presentation_variant: presentationVariant,
        partnership_matches: includePartnership ? partnershipMatches ?? [] : [],
      }));
      return { message: "Задание формирования презентации создано. Ссылка на PPTX появится в карточке после сборки.", queued: true };
    } catch (failure) {
      const needsRecheck = failure instanceof Error && failure.message === "facts_require_freshness_check";
      if (needsRecheck) {
        try {
          const refreshed = await api<ChangeSet>(`/runs/${run.run_id}/changes`);
          setComparison(refreshed);
          if (!refreshed.recheck_fact_ids?.length) {
            const message = "Список сведений для сверки недоступен. Перезапустите backend с текущей версией и обновите страницу.";
            setError(message);
            return { message };
          }
        } catch (refreshFailure) {
          const message = "Не удалось загрузить сведения для сверки. " + errorText(refreshFailure);
          setError(message);
          return { message };
        }
      }
      const message = errorText(failure); setError(message); return { message, needsRecheck };
    }
    finally { setSaving(false); }
  }

  async function openPreview() {
    if (!material) return false;
    const currentMaterial = material;
    setPreviewOpen(true);
    setPreviewLoading(true); setError("");
    setPreviewError(""); setPreviewActionError(""); setPresentationPreview(null); setBriefPreview("");
    setSavedMaterialId(null);
    try {
      const previewRequest = downloadFormat === "docx"
        ? fetch(currentMaterial.brief_url, { cache: "no-store" }).then(async (response) => {
          if (!response.ok) throw new Error("presentation_preview_unavailable");
          return response.text();
        })
        : api<PptxPreviewData>(`/materials/${encodeURIComponent(currentMaterial.material_id)}/preview`);
      const [previewResult, reportsResult] = await Promise.allSettled([previewRequest, api<SavedMaterial[]>("/materials")]);
      if (reportsResult.status === "fulfilled" && reportsResult.value.some((report) => report.id === currentMaterial.material_id)) {
        setSavedMaterialId(currentMaterial.material_id);
      }
      if (previewResult.status === "rejected") throw previewResult.reason;
      if (downloadFormat === "docx") setBriefPreview(previewResult.value as string);
      else {
        const result = previewResult.value as PptxPreviewData;
        if (!result.slides.length) throw new Error("presentation_preview_unavailable");
        setPresentationPreview(result);
      }
      return true;
    } catch (failure) {
      setPreviewError(failure instanceof TypeError
        ? "Нет связи с сервером. Проверьте, что backend запущен на порту 8000."
        : errorText(failure));
      return false;
    } finally { setPreviewLoading(false); }
  }

  async function downloadPresentation() {
    if (!material) return;
    setDownloadingPresentation(true);
    setError("");
    try {
      await downloadFile(downloadUrl ?? material.download_url,
        `${university.name} — отчёт.${downloadFormat}`);
    } catch (failure) {
      setError(errorText(failure));
      setPreviewActionError(errorText(failure));
    } finally {
      setDownloadingPresentation(false);
    }
  }

  async function addPresentationToReports() {
    if (!material || downloadFormat !== "pptx") return;
    setSavingToReports(true);
    setPreviewActionError("");
    try {
      await api<{ saved: boolean }>(`/materials/${encodeURIComponent(material.material_id)}/report`, { method: "PUT" });
      setSavedMaterialId(material.material_id);
    } catch (failure) {
      setPreviewActionError(errorText(failure));
    } finally {
      setSavingToReports(false);
    }
  }

  async function openOrGeneratePresentation() {
    setError("");
    setFormatPickerOpen(true);
  }

  async function continueToPreview() {
    if (material?.presentation_variant === presentationVariant) {
      if (await openPreview()) setFormatPickerOpen(false);
      return;
    }
    if (busy && job?.task_type === "material") {
      setPreviewAfterGenerationFrom(material?.material_id ?? null);
      setPreviewAfterGeneration(true);
      return;
    }
    if (!run || (!accepted && !material)) {
      setError(!run
        ? "Сначала нажмите «Найти и обновить информацию», чтобы собрать сведения."
        : "Чтобы сформировать презентацию, проверьте кандидатов и примите факты из первоисточников.");
      if (run) showCandidates();
      return;
    }
    if (busy) {
      setError("Дождитесь завершения текущего задания, затем откройте предпросмотр.");
      return;
    }
    setError("");
    setPreviewAfterGenerationFrom(material?.material_id ?? null);
    setPreviewAfterGeneration(true);
    const result = await generate();
    if (!result.queued) {
      setPreviewAfterGeneration(false);
      setPreviewAfterGenerationFrom(null);
      setError(result.message);
      if (result.needsRecheck) {
        setFormatPickerOpen(false);
        showRecheck();
      }
    }
  }

  function closeFormatPicker() {
    setFormatPickerOpen(false);
    setPreviewAfterGeneration(false);
    setPreviewAfterGenerationFrom(null);
  }

  useEffect(() => {
    if (!previewAfterGeneration || !material || material.material_id === previewAfterGenerationFrom) return;
    setPreviewAfterGeneration(false);
    setPreviewAfterGenerationFrom(null);
    void openPreview().then((opened) => { if (opened) setFormatPickerOpen(false); });
  }, [material, previewAfterGeneration, previewAfterGenerationFrom]);

  function showRecheck() {
    if (recheckRef.current) recheckRef.current.open = true;
    recheckRef.current?.scrollIntoView({ block: "center" });
    recheckRef.current?.focus({ preventScroll: true });
  }
  useImperativeHandle(ref, () => ({ generate, refresh, showRecheck, openReportOptions: () => { setError(""); setFormatPickerOpen(true); } }));

  return <section className="content-card research-panel" aria-label="Актуализация и проверка фактов">
    <div className="content-card__heading"><div><h2>Сбор и материалы</h2><span>Обновите сведения или подготовьте документ к встрече</span></div></div>
    <div className="research-controls">
      <label>Официальный сайт<input type="url" value={website} disabled={Boolean(card) || Boolean(busy)}
        onChange={(event) => setWebsite(event.target.value)} placeholder="https://university.ru" aria-label="Официальный сайт" /></label>
    </div>
    {suggestWebsite && <p className="research-website-hint" role="status">{!website.trim()
      ? "Официальный сайт не указан. Если автоматический поиск не нашёл адрес, укажите ссылку при создании карточки."
      : "По доступным страницам не удалось найти достаточно сведений. Если указан неофициальный адрес, при создании карточки укажите официальный сайт — так поиск сможет найти больше информации."}</p>}
    <details className="research-settings">
      <summary>Настроить материал и встречу</summary>
      <fieldset className="research-meeting" disabled={Boolean(busy)}>
        <div className="research-controls">
          <label>Формат материала<select value={presentationVariant}
            onChange={(event) => setPresentationVariant(event.target.value as "compact" | "detailed")}>
            <option value="compact">Краткая карточка — 3 слайда</option>
            <option value="detailed">Подробная справка — все разделы</option>
          </select></label>
          <label>Цель встречи<input value={meetingGoal} onChange={(event) => setMeetingGoal(event.target.value)}
            maxLength={1000} placeholder="Например: обсудить подготовку ИТ-специалистов" /></label>
          <label>Дата встречи<input type="date" value={meetingDate} onChange={(event) => setMeetingDate(event.target.value)} /></label>
        </div>
      </fieldset>
    </details>
    <div className="research-action-stack">
      <button className="button button--primary" type="button" disabled={Boolean(busy) || !website.trim()} onClick={() => { void refresh(); }}>Обновить сведения</button>
      <button className="button button--soft" type="button" disabled={Boolean(busy)}
        onClick={() => { void openOrGeneratePresentation(); }}>{busy && job?.task_type === "material" ? "Готовим материал…" : material?.presentation_variant === presentationVariant ? "Открыть материал" : material ? "Обновить материал" : "Подготовить материал"}</button>
    </div>
    {factsToRecheck.length > 0 && <details ref={recheckRef} tabIndex={-1} className="research-settings research-settings--recheck">
      <summary>Повторная проверка перед экспортом <span>{factsToRecheck.length}</span></summary>
      <fieldset className="research-meeting" disabled={Boolean(busy)}>
        <p>Сверьте принятые сведения с источниками. Если значение изменилось, обновите карточку перед экспортом.</p>
        <details><summary>Сведения для сверки ({factsToRecheck.length})</summary>
        <ul>{factsToRecheck.map((fact) => <li key={fact.fact_id}>
          <strong>{fact.subject.person_name ?? fact.subject.entity_name ?? fact.contact_owner?.label ?? "Учреждение"}</strong>
          {" · "}{fields[fact.field] ?? fact.field}{" · "}{fact.value}
          {fact.evidence.map((source, index) => <div key={index}>
            <a href={source.page_number ? `${source.url.split("#")[0]}#page=${source.page_number}` : source.url}
              target="_blank" rel="noreferrer">Источник{source.page_number ? ` · страница ${source.page_number}` : ""}</a>
          </div>)}
        </li>)}</ul>
      </details>
      <div className="research-controls">
        <label>Проверяющий<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} maxLength={160} /></label>
        <button className="button button--soft" type="button" disabled={!reviewer.trim() || !recheckConfirmed}
          onClick={() => { void recheck(); }}>Подтвердить актуальность</button>
      </div>
      <label className="research-ack"><input type="checkbox" checked={recheckConfirmed}
        onChange={(event) => setRecheckConfirmed(event.target.checked)} /> Я повторно сверил(а) все указанные сведения, значения актуальны</label>
      </fieldset>
    </details>}
    {job && <div className="research-progress" role="status"><span>{job.message}</span><progress value={job.progress} max={100} /></div>}
    {error && <p className="research-error" role="alert">{error}</p>}
    {run && <p className="research-summary">В последнем сборе: {run.facts.length} кандидатов · на проверке: {pending} · в подтверждённой карточке: {accepted} · отклонено: {rejected} · {run.status === "partial" ? "сбор частичный" : run.status === "failed" ? "сбор не выполнен; прежние принятые сведения сохранены" : "результат сохранён"}.
      {!showFacts && <button className="text-button" type="button" onClick={onShowFacts}>Проверить факты</button>}</p>}
    {run && (issueCounts.size > 0 || stageErrors.length > 0 || run.parser_errors.length > 0 || (run.parser_warnings?.length ?? 0) > 0 || gaps.length > 0) &&
      <details className="research-diagnostics"><summary>Подробности последнего сбора</summary>
        <p>Автоматическая проверка не подтверждает полноту сведений. Сверьте нужные факты по первоисточникам.</p>
        {issueCounts.size > 0 && <ul>{[...issueCounts].map(([code, count]) => <li key={code}>{issueMessage(code)} Кандидатов: {count}.</li>)}</ul>}
        {stageErrors.length > 0 && <ul>{[...new Set(stageErrors)].map((code) => <li key={code}>{errorText(new Error(code.replace(/^0\.[23]:/, "")))}</li>)}</ul>}
        {run.parser_errors.length > 0 && <><p>Не удалось получить часть источников: {run.parser_errors.length}.</p><ul>{run.parser_errors.filter((code) => code.startsWith("pdf_") || code.includes("pdf_")).map((code, index) => <li key={index}>{errorText(new Error(code))}</li>)}</ul></>}
        {run.parser_warnings?.some((warning) => warning.startsWith("PDF:")) && <ul>{run.parser_warnings.filter((warning) => warning.startsWith("PDF:")).map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
        {(run.parser_warnings?.length ?? 0) > 0 && <p>Парсер сообщил об ограничениях: {run.parser_warnings.length}. Проверьте, все ли нужные страницы обработаны.</p>}
        {gaps.length > 0 && <><p>Пропуски, отмеченные моделью (не доказывают отсутствие сведений):</p><ul>{gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul></>}
      </details>}
    {showFacts && run && reviewOutlet && createPortal(<section className="content-card review-workspace" aria-label="Проверка найденных сведений">
      <div className="research-review-tabs" role="tablist" aria-label="Разделы проверки">
        <button className={`research-review-tab${reviewTab === "candidates" ? " research-review-tab--active" : ""}`} type="button" role="tab"
          id="research-candidates-tab" aria-selected={reviewTab === "candidates"} aria-controls="research-candidates-panel" onClick={() => setReviewTab("candidates")}>
          К принятию <span>{pending}</span>
        </button>
        <button className={`research-review-tab${reviewTab === "conflicts" ? " research-review-tab--active" : ""}`} type="button" role="tab"
          id="research-conflicts-tab" aria-selected={reviewTab === "conflicts"} aria-controls="research-conflicts-panel" onClick={() => setReviewTab("conflicts")}>
          Конфликты <span>{conflictCount}</span>
        </button>
        <button className={`research-review-tab${reviewTab === "accepted" ? " research-review-tab--active" : ""}`} type="button" role="tab"
          id="research-accepted-tab" aria-selected={reviewTab === "accepted"} aria-controls="research-accepted-panel" onClick={() => setReviewTab("accepted")}>
          Принято <span>{accepted}</span>
        </button>
      </div>
      {reviewTab === "conflicts" ? <div id="research-conflicts-panel" role="tabpanel" aria-labelledby="research-conflicts-tab">
        {currentComparison ? <ResearchChanges data={currentComparison} labels={fields} selected={selected} onSelect={setSelected} busy={Boolean(busy)} onlyConflicts />
          : <p className="research-review-empty" role="status">Сравнение появится после завершения проверки данных.</p>}
        {selected.length > 0 && <button className="text-button" type="button" onClick={() => setReviewTab("candidates")}>
          Перейти к принятию выбранных сведений ({selected.length})</button>}
        {history.length > 0 && <details className="research-history"><summary>История сборов ({history.length})</summary>
          {history.map((item) => <p key={item.run_id}>{new Date(item.created_at).toLocaleString("ru-RU")} · принятых сведений: {item.approved_count}
            {item.run_id === run.run_id ? " · текущий сбор" : <button className="text-button" type="button" onClick={() => {
              void api<ChangeSet>(`/runs/${item.run_id}/changes`).then(setPastChanges).catch((failure) => setError(errorText(failure)));
            }}>Просмотреть</button>}</p>)}
          {pastChanges && <><p>Архивный сбор: только просмотр.</p><ResearchChanges data={pastChanges} labels={fields}/>
            <button className="text-button" type="button" onClick={() => setPastChanges(null)}>Закрыть архивный сбор</button></>}
        </details>}
      </div> : reviewTab === "accepted" ? <div id="research-accepted-panel" role="tabpanel" aria-labelledby="research-accepted-tab">
        {acceptedFactGroups.length ? <>
          <p className="research-review-empty">Выберите принятые сведения, чтобы отменить их принятие. Если они заменили прежние данные, прежние значения восстановятся.</p>
          <div className="research-facts">{acceptedFactGroups.map((group) => <article className="research-fact research-fact--group" key={group.key}>
            <header className="research-fact__group-heading">
              <label className="research-fact__group-select"><input type="checkbox" checked={group.facts.every((fact) => selected.includes(fact.fact_id))}
                disabled={Boolean(busy)} aria-label={`Выбрать принятые сведения: ${group.title}`}
                onChange={(event) => setSelected((current) => event.target.checked
                  ? [...new Set([...current, ...group.facts.map((fact) => fact.fact_id)])]
                  : current.filter((id) => !group.facts.some((fact) => fact.fact_id === id)))} />
                <span><strong>{group.title}</strong><small>{group.description}</small></span>
              </label>
              <span className="research-fact__group-count">{group.facts.length} сведений</span>
            </header>
            <div className="research-fact__items">{group.facts.map((fact) => <section className="research-fact__item" key={fact.fact_id}>
              <label className="research-fact__item-select"><input type="checkbox" checked={selected.includes(fact.fact_id)} disabled={Boolean(busy)}
                aria-label={`Выбрать для отмены: ${group.title} — ${fields[fact.field] ?? fact.field}`}
                onChange={(event) => setSelected((current) => event.target.checked
                  ? [...current, fact.fact_id] : current.filter((id) => id !== fact.fact_id))} />
                <span><strong>{fields[fact.field] ?? fact.field}</strong><span>{fact.value}</span></span>
                <span className="research-status research-status--verified">Принято</span>
              </label>
              {fact.subject.department && <p>Подразделение: {fact.subject.department}</p>}
              <details><summary>Источники ({fact.evidence.length})</summary>{fact.evidence.map((evidence, index) => <div className="research-evidence" key={index}>
                <a href={evidence.page_number ? `${evidence.url.split("#")[0]}#page=${evidence.page_number}` : evidence.url} target="_blank" rel="noreferrer">{evidence.url}</a>
                <blockquote>{evidence.quote}</blockquote>
              </div>)}</details>
            </section>)}</div>
          </article>)}</div>
          <div className="research-review">
            <label>Проверяющий<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} placeholder="Имя и фамилия" /></label>
            <label>Основание отмены<input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Почему отменяется принятие" /></label>
            <div className="research-controls"><button className="button button--soft" type="button" disabled={Boolean(busy) || !selectedAcceptedCount}
              onClick={() => { void review("revoke"); }}>Отменить принятие ({selectedAcceptedCount})</button></div>
          </div>
        </> : <p className="research-review-empty" role="status">Пока нет принятых сведений.</p>}
      </div> : <div id="research-candidates-panel" role="tabpanel" aria-labelledby="research-candidates-tab">
      <details className="research-tools">
        <summary>Быстрые действия со списком</summary>
        <div className="research-tool-actions">
      <button className="text-button" type="button" disabled={Boolean(busy)} onClick={() => setSelected(run.facts
        .filter((fact) => fact.verification_status === "pending" && fact.validation_status === "passed" && !fact.issues.length)
        .map((fact) => fact.fact_id))}>Выбрать все факты без ошибок</button>
      <button className="text-button" type="button" disabled={Boolean(busy)} onClick={() => setSelected(run.facts
        .filter((fact) => fact.verification_status === "pending" && fact.validation_status === "passed" && !fact.issues.length
          && fact.subject.person_name && !fact.subject.department)
        .map((fact) => fact.fact_id))}>Выбрать сотрудников без привязки к подразделению</button>
      {rejected > 0 && <button className="text-button" type="button" onClick={() => setShowRejected((value) => !value)}>
        {showRejected ? "Скрыть" : "Показать"} автоматически отклонённые факты ({rejected})</button>}
        </div>
      </details>
      <div className="research-facts">{visibleFactGroups.map((group) => {
        const eligibleIds = group.facts.filter((fact) => fact.verification_status === "pending"
          && fact.validation_status === "passed" && !fact.issues.length).map((fact) => fact.fact_id);
        const groupSelected = eligibleIds.length > 0 && eligibleIds.every((id) => selected.includes(id));
        const pendingCount = group.facts.filter((fact) => fact.verification_status === "pending").length;
        return <article className={`research-fact${group.facts.length > 1 ? " research-fact--group" : ""}`} key={group.key}>
          {group.facts.length > 1 && <header className="research-fact__group-heading">
            <label className="research-fact__group-select"><input type="checkbox" checked={groupSelected} disabled={Boolean(busy) || eligibleIds.length === 0}
              aria-label={`Выбрать проверяемые факты: ${group.title}`}
              onChange={(event) => setSelected((current) => event.target.checked
                ? [...new Set([...current, ...eligibleIds])] : current.filter((id) => !eligibleIds.includes(id)))} />
              <span><strong>{group.title}</strong><small>{group.description}</small></span>
            </label>
            <span className="research-fact__group-count">{group.facts.length} сведений{pendingCount ? ` · на проверке: ${pendingCount}` : ""}</span>
          </header>}
          <div className="research-fact__items">{group.facts.map((fact) => <section className="research-fact__item" key={fact.fact_id}>
            <label className="research-fact__item-select"><input type="checkbox" checked={selected.includes(fact.fact_id)} disabled={Boolean(busy)}
              aria-label={`Выбрать: ${group.title} — ${fields[fact.field] ?? fact.field}`}
              onChange={(event) => setSelected((current) => event.target.checked
                ? [...current, fact.fact_id] : current.filter((id) => id !== fact.fact_id))} />
              <span><strong>{group.facts.length === 1 ? group.title : fields[fact.field] ?? fact.field}</strong><span>{group.facts.length === 1 ? `${fields[fact.field] ?? fact.field}: ${fact.value}` : fact.value}</span></span>
              <span className={`research-status research-status--${fact.verification_status}`}>{statuses[fact.verification_status]}</span>
            </label>
            {fact.subject.department && <p>Подразделение: {fact.subject.department}</p>}
            {fact.metric_context && <p>Период: {fact.metric_context.period} · Состав: {fact.metric_context.scope} · Единица: {fact.metric_context.unit}{fact.metric_context.coverage ? ` · Охват: ${fact.metric_context.coverage}` : ""}</p>}
            {fact.issues.length > 0 && <details className="research-fact__validation">
              <summary>Требует уточнения</summary>
              <div>{fact.issues.map((code) => <p key={code}>{issueMessage(code)}</p>)}<small>Принятие заблокировано до уточнения сведений.</small></div>
            </details>}
            <details className="research-fact__sources"><summary>Источники и цитаты ({fact.evidence.length})</summary>{fact.evidence.map((evidence, index) => <div className="research-evidence" key={index}>
              <a href={evidence.page_number ? `${evidence.url.split("#")[0]}#page=${evidence.page_number}` : evidence.url} target="_blank" rel="noreferrer">{evidence.page_number ? `PDF · страница ${evidence.page_number} · ` : ""}{evidence.url}</a><small>Получено: {new Date(evidence.retrieved_at).toLocaleString("ru-RU")}</small><blockquote>{evidence.quote}</blockquote>
            </div>)}</details>
          </section>)}</div>
        </article>;
      })}</div>
      <div className="research-review">
        <label>Проверяющий<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} placeholder="Имя и фамилия" /></label>
        <label>Основание решения<input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Что сверено или почему отклонено" /></label>
        <label className="research-ack"><input type="checkbox" checked={sourceChecked} onChange={(event) => setSourceChecked(event.target.checked)} />Я проверил(а) выбранные факты по действующим первоисточникам</label>
        {retirementIds.length > 0 && <label className="research-ack research-replacement"><input type="checkbox" checked={replacePrevious}
          onChange={(event) => setReplacePrevious(event.target.checked)} />Подтверждаю замену: прежних сведений будет архивировано {retirementIds.length}. Их значения и решения останутся в истории.</label>}
        <div className="research-controls"><button className="button button--primary" type="button" disabled={Boolean(busy) || !selectedCandidateCount} onClick={() => { void review("accept"); }}>Принять выбранные ({selectedCandidateCount})</button>
        <button className="button button--soft" type="button" disabled={Boolean(busy) || !selectedCandidateCount} onClick={() => { void review("reject"); }}>Отклонить выбранные</button></div>
      </div>
      </div>}</section>, reviewOutlet)}
    {formatPickerOpen && typeof document !== "undefined" && createPortal(<div className="report-preview-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeFormatPicker(); }}>
      <section className="report-format-dialog" role="dialog" aria-modal="true" aria-labelledby="report-format-title">
        <header className="report-preview__header">
          <div><span className="eyebrow">ОТЧЁТ К ВСТРЕЧЕ</span><h2 id="report-format-title">Выберите формат файла</h2></div>
          <button className="icon-button" type="button" onClick={closeFormatPicker} aria-label="Закрыть выбор формата"><Icon name="close" size={19} /></button>
        </header>
        <div className="report-format-options">
          <label className={`report-format-option${downloadFormat === "pptx" ? " report-format-option--selected" : ""}`}>
            <input type="radio" name="report-format" value="pptx" checked={downloadFormat === "pptx"} onChange={() => setDownloadFormat("pptx")} />
            <span><strong>PowerPoint</strong><small>Презентация (.pptx)</small></span>
          </label>
          <label className={`report-format-option${downloadFormat === "docx" ? " report-format-option--selected" : ""}`}>
            <input type="radio" name="report-format" value="docx" checked={downloadFormat === "docx"} onChange={() => setDownloadFormat("docx")} />
            <span><strong>Word</strong><small>Документ (.docx)</small></span>
          </label>
        </div>
        {!material && run && accepted > 0 && <p className="report-format-hint">Отчёт ещё не сформирован. После выбора формата он подготовится и откроется для просмотра.</p>}
        {busy && job?.task_type === "material" && <p className="report-format-hint" role="status">{job.message} · {job.progress}%</p>}
        {error && <p className="research-error" role="alert">{error}</p>}
        <div className="report-format-actions">
          <button className="button button--soft" type="button" onClick={closeFormatPicker}>Отмена</button>
          <button className="button button--primary" type="button" disabled={previewLoading || previewAfterGeneration || (!material && (!run || !accepted)) || (busy && job?.task_type !== "material")} onClick={() => { void continueToPreview(); }}>
            {previewLoading ? "Открываем…" : previewAfterGeneration || (busy && job?.task_type === "material") ? "Ждём завершения…" : material ? "Открыть предпросмотр" : !run || !accepted ? "Нет проверенных данных" : "Сформировать и просмотреть"}
          </button>
        </div>
      </section>
    </div>, document.body)}
    {previewOpen && typeof document !== "undefined" && createPortal(<div className="report-preview-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setPreviewOpen(false); }}>
      <section className="report-preview" role="dialog" aria-modal="true" aria-labelledby="report-preview-title">
        <header className="report-preview__header">
          <div><span className="eyebrow">ПРЕДПРОСМОТР {downloadFormat.toUpperCase()}</span><h2 id="report-preview-title">Отчёт к встрече: {university.name}</h2></div>
          <button className="icon-button" type="button" onClick={() => setPreviewOpen(false)} aria-label="Закрыть предпросмотр"><Icon name="close" size={19} /></button>
        </header>
        <div className="report-preview__content">
          {previewLoading && <div className="reports-empty" role="status"><Icon name="file" size={24} /><strong>Загружаем предпросмотр</strong><span>Открываем выбранный формат отчёта.</span></div>}
          {previewError && <div className="reports-empty" role="alert"><Icon name="file" size={24} /><strong>Предпросмотр не загрузился</strong><span>{previewError}</span><button className="button button--soft" type="button" disabled={previewLoading} onClick={() => { void openPreview(); }}>Повторить</button></div>}
          {!previewLoading && !previewError && presentationPreview && <PptxPreview preview={presentationPreview} />}
          {!previewLoading && !previewError && downloadFormat === "docx" && briefPreview.split("\n").map((line, index) => {
            if (line.startsWith("# ")) return <h3 key={index}>{line.slice(2)}</h3>;
            if (line.startsWith("## ")) return <h4 key={index}>{line.slice(3)}</h4>;
            return line.trim() ? <p key={index}>{line}</p> : <br key={index} />;
          })}
        </div>
        <footer className="report-preview__actions">
          <div className="report-preview__action-copy">
            <span>{downloadFormat === "pptx" ? presentationPreview ? `${presentationPreview.slides.length} слайдов · файл PPTX` : "Файл PPTX" : "Предпросмотр справки · файл DOCX"}</span>
            {previewActionError && <span className="report-preview__action-error" role="alert">{previewActionError}</span>}
          </div>
          <div className="report-preview__button-group">
            {downloadFormat === "pptx" && <button className="button button--soft" type="button" disabled={!presentationPreview || savingToReports || savedMaterialId === material?.material_id} onClick={() => { void addPresentationToReports(); }}>
              <Icon name={savedMaterialId === material?.material_id ? "check" : "plus"} size={16} />
              {savingToReports ? "Добавляем…" : savedMaterialId === material?.material_id ? "Уже в отчётах" : "Добавить в отчёты"}
            </button>}
            <button className="button button--primary" type="button" disabled={!material || previewLoading || downloadingPresentation} onClick={() => { void downloadPresentation(); }}><Icon name="download" size={16} />{downloadingPresentation ? "Скачиваем…" : "Скачать на компьютер"}</button>
          </div>
        </footer>
      </section>
    </div>, document.body)}
  </section>;
});

