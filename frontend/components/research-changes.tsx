"use client";

import { useState } from "react";
import { type ChangeSet, type Fact } from "@/lib/api";

const kinds = {new: "Новое", changed: "Изменение", head_change: "Возможная смена руководителя", unchanged: "Без изменений", not_confirmed: "Не подтверждено повторным сбором"};

function Sources({fact}: {fact: Fact}) {
  return <details className="change-sources"><summary>Источники ({fact.evidence.length})</summary>{fact.evidence.map((e, i) => <div className="research-evidence" key={i}>
    <a href={e.page_number ? `${e.url.split("#")[0]}#page=${e.page_number}` : e.url} target="_blank" rel="noreferrer">
      {e.page_number ? `PDF · страница ${e.page_number} · ` : ""}{e.url}</a>
    <small>Получено: {new Date(e.retrieved_at).toLocaleString("ru-RU")}</small><blockquote>{e.quote}</blockquote>
  </div>)}</details>;
}

function Value({fact, labels, archived = false}: {fact: Fact; labels: Record<string, string>; archived?: boolean}) {
  return <div className="change-value"><span>{labels[fact.field] ?? fact.field}: {fact.value}</span>
    {fact.metric_context && <small>Период: {fact.metric_context.period}; состав: {fact.metric_context.scope}; единица: {fact.metric_context.unit}
      {fact.metric_context.coverage ? `; охват: ${fact.metric_context.coverage}` : ""}</small>}
    <small>{archived ? "Архив · ранее принято" : fact.verification_status === "verified" ? "Принято" : "На проверке"}
      {fact.last_checked_at ? ` · проверено ${new Date(fact.last_checked_at).toLocaleString("ru-RU")}` : ""}</small><Sources fact={fact}/></div>;
}

export function ApprovedCard({facts, labels}: {facts: Fact[]; labels: Record<string, string>}) {
  const groups = new Map<string, Fact[]>();
  for (const fact of facts) {
    const name = [fact.subject.person_name ?? fact.subject.entity_name ?? fact.contact_owner?.label ?? "Учреждение", fact.subject.department].filter(Boolean).join(" · ");
    groups.set(name, [...(groups.get(name) ?? []), fact]);
  }
  return <details className="approved-card"><summary>Подтверждённая карточка ({facts.length})</summary>
    <p>Здесь показаны принятые сведения. Новые кандидаты не заменяют их до решения проверяющего.</p>
    {[...groups].map(([name, items]) => <section key={name}><h3>{name}</h3>{items.map((fact) => <Value key={fact.fact_id} fact={fact} labels={labels}/>)}</section>)}
    {!facts.length && <p>Подтверждённых сведений пока нет.</p>}
  </details>;
}

export function ResearchChanges({data, labels, selected = [], onSelect, busy = false}: {
  data: ChangeSet; labels: Record<string, string>; selected?: string[]; onSelect?: (ids: string[]) => void; busy?: boolean;
}) {
  const [showUnchanged, setShowUnchanged] = useState(false);
  return <section className="research-changes" aria-label="Сравнение сведений">
    <h3>Изменения относительно подтверждённой карточки</h3>
    <p className="change-counts">Новые: {data.counts.new} · изменённые поля: {data.counts.changed} · возможная смена руководителя: {data.counts.head_change} · без изменений: {data.counts.unchanged} · не подтверждено: {data.counts.not_confirmed}</p>
    <p>{data.warning}</p>
    {data.counts.unchanged > 0 && <button type="button" className="text-button" onClick={() => setShowUnchanged(!showUnchanged)}>{showUnchanged ? "Скрыть" : "Показать"} сведения без изменений</button>}
    {data.entries.filter((item) => showUnchanged || item.kind !== "unchanged").map((change) => {
      const ids = change.after.filter((f) => f.verification_status === "pending").map((f) => f.fact_id);
      return <article className={`research-change research-change--${change.kind}`} key={change.change_id}>
        <div className="research-change__heading"><strong>{change.subject_label}</strong><span>{kinds[change.kind]}</span></div>
        {change.kind === "head_change" && <p>Предлагается новая связка руководителя. Прежние сведения будут архивированы только после явного подтверждения замены.</p>}
        <div className="change-columns"><div><h4>Было принято</h4>{change.before.length ? change.before.map((fact) => <Value key={fact.fact_id} fact={fact} labels={labels}
          archived={!data.approved_facts.some((active) => active.fact_id === fact.fact_id)}/>) : <p>Принятого значения нет.</p>}</div>
          <div><h4>Получено в новом сборе</h4>{change.after.length ? change.after.map((fact) => <Value key={fact.fact_id} fact={fact} labels={labels}/>) : <p>Пригодный кандидат не получен. Прежние сведения сохранены в карточке.</p>}</div></div>
        {onSelect && ids.length > 0 && <button type="button" className="text-button" disabled={busy}
          onClick={() => onSelect([...new Set([...selected, ...ids])])}>Выбрать эти сведения для проверки ({ids.length})</button>}
      </article>;
    })}
  </section>;
}
