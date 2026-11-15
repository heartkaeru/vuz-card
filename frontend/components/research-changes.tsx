"use client";

import { type ChangeSet, type Fact, type FactChange } from "@/lib/api";

const kinds = {new: "Новое", changed: "Изменение", head_change: "Возможная смена руководителя", unchanged: "Без изменений", not_confirmed: "Не подтверждено повторным сбором"};

function Sources({fact}: {fact: Fact}) {
  if (fact.origin === "manual_edit") return <small>Подтверждено сотрудником в чате.</small>;
  return <details className="change-sources"><summary>Источники ({fact.evidence.length})</summary>{fact.evidence.map((e, i) => <div className="research-evidence" key={i}>
    <a href={e.page_number ? `${e.url.split("#")[0]}#page=${e.page_number}` : e.url} target="_blank" rel="noreferrer">
      {e.page_number ? `PDF · страница ${e.page_number} · ` : ""}{e.url}</a>
    <small>Получено: {new Date(e.retrieved_at).toLocaleString("ru-RU")}</small><blockquote>{e.quote}</blockquote>
  </div>)}</details>;
}

function Value({fact, labels, archived = false, reconfirmed = false, selection}: {
  fact: Fact; labels: Record<string, string>; archived?: boolean; reconfirmed?: boolean;
  selection?: {checked: boolean; disabled: boolean; toggle: () => void};
}) {
  const text = `${labels[fact.field] ?? fact.field}: ${fact.value}`;
  return <div className="change-value">{selection ? <label className="change-selection">
    <input type="checkbox" checked={selection.checked} disabled={selection.disabled} onChange={selection.toggle}/>
    <span>{text}</span></label> : <span>{text}</span>}
    {fact.metric_context && <small>Период: {fact.metric_context.period}; состав: {fact.metric_context.scope}; единица: {fact.metric_context.unit}
      {fact.metric_context.coverage ? `; охват: ${fact.metric_context.coverage}` : ""}</small>}
    <small>{archived ? "Архив · ранее принято" : reconfirmed ? "Подтверждено повторным сбором" : fact.verification_status === "verified" ? "Принято" : "На проверке"}
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
  const reconfirmed = data.reconfirmed_facts ?? {};
  const matchedIds = new Set(Object.values(reconfirmed).flat());
  const approvedIds = new Set(data.approved_facts.map((fact) => fact.fact_id));
  const selectedIds = new Set(selected);
  const isPending = (fact: Fact) => fact.verification_status === "pending" && !matchedIds.has(fact.fact_id);
  const groups = new Map<string, {label: string; changes: FactChange[]}>();
  const missing = data.entries.filter((change) => change.kind === "not_confirmed");
  const refreshed = data.approved_facts.filter((fact) => Object.hasOwn(reconfirmed, fact.fact_id));
  for (const change of data.entries) {
    if (change.kind === "not_confirmed" || change.after.every((fact) => matchedIds.has(fact.fact_id))
      || (onSelect && !change.after.some(isPending))) continue;
    const group = groups.get(change.group_id);
    if (group) group.changes.push(change);
    else groups.set(change.group_id, {label: change.group_label, changes: [change]});
  }
  const reviewable = [...groups.values()].flatMap((group) => group.changes.flatMap((change) => change.after.filter(isPending)));
  const unscopedPeople = reviewable.filter((fact) => fact.subject.person_name && !fact.subject.department);

  function toggle(ids: string[]) {
    const next = new Set(selectedIds);
    const remove = ids.every((id) => next.has(id));
    for (const id of ids) {
      if (remove) next.delete(id);
      else next.add(id);
    }
    onSelect?.([...next]);
  }

  return <section className="research-changes" aria-label="Сравнение сведений">
    <h3>{onSelect ? "Сведения на сверку" : "Результаты сбора"}</h3>
    {onSelect && groups.size === 0 && <p>Нет новых или изменившихся сведений, которые можно принять.</p>}
    {onSelect && groups.size > 0 && <p>Отметьте нужные сведения и примите решение внизу списка. Можно выбрать все поля объекта или указать их по отдельности.</p>}
    {onSelect && groups.size > 1 && <button type="button" className="text-button" disabled={busy}
      onClick={() => onSelect(reviewable.map((fact) => fact.fact_id))}>Выбрать все факты без ошибок</button>}
    {onSelect && unscopedPeople.length > 0 && unscopedPeople.length < reviewable.length && <button type="button" className="text-button" disabled={busy}
      onClick={() => onSelect(unscopedPeople.map((fact) => fact.fact_id))}>Выбрать сотрудников без привязки к подразделению</button>}
    {[...groups].map(([id, group]) => {
      const ids = group.changes.flatMap((change) => change.after.filter(isPending).map((fact) => fact.fact_id));
      const allSelected = ids.every((factId) => selectedIds.has(factId));
      return <section className="research-change-group" key={id}>
        <header><h4>{group.label}</h4>{onSelect && ids.length > 1 && <button type="button" className="text-button" disabled={busy}
          onClick={() => toggle(ids)}>{allSelected ? "Снять выбор" : "Выбрать все"} ({ids.length})</button>}</header>
        {group.changes.map((change) => <article className={`research-change research-change--${change.kind}`} key={change.change_id}>
          <div className="research-change__heading"><strong>{change.kind === "head_change" ? "Сведения о руководителе" : labels[change.field] ?? change.field}</strong>
            <span>{change.kind === "unchanged" && change.after.some(isPending) ? "Совпадение требует сверки" : kinds[change.kind]}</span></div>
          {change.kind === "head_change" && <p>Прежние сведения будут архивированы после подтверждения замены.</p>}
          <div className={`change-columns${change.before.length ? "" : " change-columns--new"}`}>
            {change.before.length > 0 && <div><h5>Было принято{change.kind === "head_change" ? ` · ${change.before[0].subject.person_name}` : ""}</h5>{change.before.map((fact) => <Value key={fact.fact_id} fact={fact} labels={labels}
              archived={!approvedIds.has(fact.fact_id)}/>)}</div>}
            <div><h5>Получено в новом сборе{change.kind === "head_change" ? ` · ${change.after[0].subject.person_name}` : ""}</h5>{change.after.map((fact) => <Value key={fact.fact_id} fact={fact} labels={labels}
              selection={onSelect && isPending(fact) ? {checked: selectedIds.has(fact.fact_id), disabled: busy, toggle: () => toggle([fact.fact_id])} : undefined}/>)}</div>
          </div>
        </article>)}
      </section>;
    })}
    {refreshed.length > 0 && <details className="research-change-detail"><summary>Подтверждено повторным сбором ({refreshed.length})</summary>
      {refreshed.map((fact) => <div key={fact.fact_id}><strong>{fact.subject.person_name ?? fact.subject.entity_name ?? fact.contact_owner?.label}</strong>
        <Value fact={fact} labels={labels} reconfirmed/></div>)}
    </details>}
    {missing.length > 0 && <details className="research-change-detail"><summary>Не найдено в повторном сборе ({missing.length})</summary>
      <p>{data.warning}</p>{missing.map((change) => <div key={change.change_id}><strong>{change.subject_label}</strong>
        {change.before.map((fact) => <Value key={fact.fact_id} fact={fact} labels={labels}/>)}</div>)}
    </details>}
  </section>;
}
