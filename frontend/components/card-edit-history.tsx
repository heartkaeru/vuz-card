"use client";

import { useEffect, useState } from "react";
import { api, errorText, type ChangeSet, type Fact, type ResearchHistory } from "@/lib/api";

type EditEntry = {
  id: string;
  changed_at: string;
  author: string;
  author_type: "human" | "ai";
  field: string;
  before: string;
  after: string;
};

const fieldLabels: Record<string, string> = {
  "person.name": "ФИО",
  "appointment.position": "Должность",
  "appointment.status": "Статус назначения",
  "appointment.department": "Подразделение",
  "appointment.responsibilities": "Зона ответственности",
  "contact.phone": "Телефон",
  "contact.email": "Почта",
  "person.career": "Биография",
  "person.education": "Образование",
  "person.degree": "Учёная степень",
  "person.academic_title": "Учёное звание",
  "person.interests": "Научные интересы",
  "appointment.since": "Дата назначения",
  "institution.profile": "Профиль учреждения",
  "institution.type": "Тип учреждения",
  "institution.location": "Местонахождение",
  "institution.founded": "Год основания",
  "institution.education_areas": "Направления подготовки",
  "institution.education_levels": "Уровни образования",
  "institution.students": "Обучающиеся",
  "institution.structure": "Структура и филиалы",
  "project.name": "Название проекта",
  "project.program": "Программа проекта",
  "project.role": "Роль учреждения",
  "project.status": "Статус проекта",
  "project.period": "Период проекта",
  "project.description": "Описание проекта",
  "event.name": "Название события",
  "event.type": "Вид события",
  "event.date": "Дата события",
  "event.status": "Статус события",
  "event.description": "Описание события",
};

function factValue(facts: Fact[]) {
  return facts.map((fact) => fact.value).join("\n") || "—";
}

function editEntries(run: ResearchHistory, changes: ChangeSet): EditEntry[] {
  const approved = new Set(changes.approved_facts.map((fact) => fact.fact_id));
  return changes.entries.flatMap((change) => {
    if (change.kind === "unchanged") return [];
    const after = change.after.filter((fact) => approved.has(fact.fact_id));
    if (!after.length) return [];
    const before = change.before;
    const afterValue = factValue(after);
    const beforeValue = factValue(before);
    if (beforeValue === afterValue) return [];
    const reviewedFact = after.find((fact) => fact.reviewed_at);
    const reviewer = after.find((fact) => fact.reviewed_by)?.reviewed_by;
    const manualEdit = after.some((fact) => fact.origin === "manual_edit");
    const human = manualEdit || Boolean(reviewer);
    return [{
      id: `${run.run_id}-${change.change_id}`,
      changed_at: reviewedFact?.reviewed_at ?? run.created_at,
      author: manualEdit ? "Пользователь" : reviewer ? `Пользователь · ${reviewer}` : "ИИ-агент",
      author_type: human ? "human" as const : "ai" as const,
      field: change.field,
      before: beforeValue,
      after: afterValue,
    }];
  });
}

export function CardEditHistory({ cardId }: { cardId: string }) {
  const [entries, setEntries] = useState<EditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    api<ResearchHistory[]>(`/cards/${encodeURIComponent(cardId)}/history`)
      .then(async (runs) => {
        const changes = await Promise.all(runs.map((run) =>
          api<ChangeSet>(`/runs/${encodeURIComponent(run.run_id)}/changes`).then((result) => editEntries(run, result))));
        if (active) setEntries(changes.flat().sort((a, b) => b.changed_at.localeCompare(a.changed_at)));
      })
      .catch((failure) => { if (active) setError(errorText(failure)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [cardId]);

  return <section className="content-card card-edit-history" aria-label="История изменений карточки">
    <div className="content-card__heading"><div><h2>История изменений</h2><span>Подтверждённые изменения из сборов, автор и прежние значения</span></div></div>
    {loading && <p className="card-edit-history__message" role="status">Загружаем историю…</p>}
    {error && <p className="research-error" role="alert">{error}</p>}
    {!loading && !error && entries.length === 0 &&
      <p className="card-edit-history__message">Подтверждённых изменений пока нет.</p>}
    {!loading && !error && entries.length > 0 && <div className="card-edit-history__scroll">
      <table>
        <thead><tr><th scope="col">Дата</th><th scope="col">Автор</th><th scope="col">Поле</th><th scope="col">Было</th><th scope="col">Стало</th></tr></thead>
        <tbody>{entries.map((entry) => <tr key={entry.id}>
          <td>{new Date(entry.changed_at).toLocaleString("ru-RU")}</td>
          <td><span className={`card-edit-history__actor card-edit-history__actor--${entry.author_type}`}>{entry.author}</span></td>
          <th scope="row">{fieldLabels[entry.field] ?? entry.field}</th>
          <td><span className="card-edit-history__value">{entry.before}</span></td>
          <td><span className="card-edit-history__value">{entry.after}</span></td>
        </tr>)}</tbody>
      </table>
    </div>}
  </section>;
}
