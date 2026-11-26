"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Icon } from "@/components/icon";
import { api, errorText, type ApiCard, type Job, type CardActionResult } from "@/lib/api";

type Turn = {
  id: number; question: string;
  answer: {
    text: string; sources: { fact_id: string; value: string; quote: string; url: string | null }[];
    cards?: ApiCard[];
    job_id?: string;
    edit?: { field: string; owner: string; before: string; after: string; status: "pending" | "applied" };
  };
};
type ActionNotice = {
  id: number;
  path: string;
  tone: "success" | "warning" | "error";
  message: string;
  needsRecheck?: boolean;
};
type AgentChatProps = {
  open: boolean;
  universityName: string;
  cardId?: string;
  mode: "general" | "card";
  onModeChange: (mode: "general" | "card") => void;
  onOpenCard: (card: ApiCard) => void;
  onClose: () => void;
  onEditApplied?: () => void;
  onCollection: (card: ApiCard, job: Job) => void;
  onCollectionQueued: (cardId: string, jobId: string) => void;
  onAction?: (action: "refresh" | "presentation") => Promise<CardActionResult>;
  onShowRecheck?: () => void;
};

function CollectionProgress({ jobId, initialText }: { jobId: string; initialText: string }) {
  const [job, setJob] = useState<Job | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;

    async function poll() {
      try {
        const next = await api<Job>(`/jobs/${encodeURIComponent(jobId)}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setJob(next);
        setUnavailable(false);
        if (next.status === "queued" || next.status === "running") {
          timer = window.setTimeout(poll, 1500);
        }
      } catch {
        if (controller.signal.aborted) return;
        setUnavailable(true);
        timer = window.setTimeout(poll, 3000);
      }
    }

    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [jobId]);

  const text = unavailable ? "Статус сбора временно недоступен. Проверяю ещё раз…"
    : job?.status === "running" ? `Сбор выполняется · ${job.progress}%. ${job.message}`
      : job?.status === "completed" || job?.status === "failed" ? job.message : initialText;
  return <p role="status">{text}</p>;
}

export function AgentChat({ open, universityName, cardId, mode, onModeChange, onOpenCard, onClose, onAction,
  onEditApplied, onCollection, onCollectionQueued, onShowRecheck }: AgentChatProps) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [pendingQuestion, setPendingQuestion] = useState("");
  const [typing, setTyping] = useState(false);
  const [typingLabel, setTypingLabel] = useState("ИИ готовит ответ…");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [actionNotices, setActionNotices] = useState<ActionNotice[]>([]);
  const requestKey = useRef<string | null>(null);
  const lastHistoryPath = useRef<string | null>(null);
  const noticeId = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const messageEndRef = useRef<HTMLDivElement>(null);
  const inCard = mode === "card";
  const path = inCard && cardId ? `/cards/${encodeURIComponent(cardId)}/chat` : "/chat/general";

  useEffect(() => {
    if (!open) return;
    if (lastHistoryPath.current !== path) {
      lastHistoryPath.current = path;
      setTurns([]);
      setError("");
    }
    const controller = new AbortController();
    setLoading(true);
    api<Turn[]>(path, { signal: controller.signal })
      .then((history) => { if (!controller.signal.aborted) setTurns(history); })
      .catch((failure) => { if (!controller.signal.aborted) showError(failure); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [open, path]);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => inputRef.current?.focus(), 320);
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onClose, loading]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, typing, actionNotices, path]);

  const visibleNotices = actionNotices.filter((notice) => notice.path === path);
  const latestRefresh = inCard && lastHistoryPath.current === path
    ? [...turns].reverse().find((turn) => turn.answer.job_id) : undefined;

  function addActionNotice(notice: Omit<ActionNotice, "id">) {
    setActionNotices((current) => [...current.slice(-19), { ...notice, id: ++noticeId.current }]);
  }

  function showError(failure: unknown) {
    setError(errorText(failure));
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || typing || loading) return;
    setPendingQuestion(message);
    setTyping(true); setTypingLabel("ИИ готовит ответ…"); setError("");
    try {
      const request_key = requestKey.current ??= crypto.randomUUID();
      const result = await api<Turn | { collection: { card: ApiCard; job: Job } }>(
        path, { method: "POST", body: JSON.stringify({ message, request_key }) },
      );
      if ("collection" in result) {
        const { card, job } = result.collection;
        const tone = job.status === "failed" ? "error" : "success";
        const message = job.status === "failed"
          ? `❗ Не удалось собрать сведения о ${card.name}: ${job.message}`
          : job.status === "completed"
            ? `✅ Карточка ${card.name} успешно сформирована.`
            : `✅ Карточка ${card.name} создана. Сбор сведений запущен.`;
        addActionNotice({ path: `/cards/${encodeURIComponent(card.id)}/chat`, tone, message });
        onCollection(card, job);
      } else {
        if (inCard && cardId && result.answer.job_id) onCollectionQueued(cardId, result.answer.job_id);
        setTurns((current) => current.some((saved) => saved.id === result.id) ? current : [...current, result].slice(-20));
      }
      requestKey.current = null;
      setDraft("");
    } catch (failure) {
      showError(failure);
      if (failure instanceof Error && failure.message === "chat_history_changed") {
        try { setTurns(await api<Turn[]>(path)); }
        catch (historyFailure) { showError(historyFailure); }
      }
    } finally { setTyping(false); setTypingLabel("ИИ готовит ответ…"); setPendingQuestion(""); }
  }

  async function runAction(action: "refresh" | "presentation") {
    if (!onAction || typing) return;
    setTyping(true);
    setTypingLabel(action === "refresh" ? "Обновляю сведения…" : "Готовлю материал…");
    setError("");
    try {
      const result = await onAction(action);
      const tone = result.queued ? "success" : result.needsRecheck ? "warning" : "error";
      const message = result.queued
        ? action === "refresh" ? "✅ Сбор сведений запущен." : "✅ Подготовка материала запущена."
        : result.needsRecheck ? `⚠️ ${result.message}` : `❗ ${result.message}`;
      addActionNotice({ path, tone, message, needsRecheck: result.needsRecheck });
    }
    catch (failure) { showError(failure); }
    finally { setTyping(false); setTypingLabel("ИИ готовит ответ…"); }
  }

  async function confirmEdit(turnId: number) {
    if (typing || loading || !inCard || !cardId) return;
    setTyping(true); setTypingLabel("Сохраняю изменение…"); setError("");
    try {
      await api(`${path}/${turnId}/confirm`, { method: "POST" });
      onEditApplied?.();
      setTurns(await api<Turn[]>(path));
    } catch (failure) { showError(failure); }
    finally { setTyping(false); setTypingLabel("ИИ готовит ответ…"); }
  }

  function getCollectionStatus(turn: Turn) {
    const collectionStartedText = "Ход сбора и результат видны в карточке.";
    const card = turn.answer.cards?.[0];
    return card && turn.answer.text.includes(collectionStartedText)
      ? `✅ Карточка ${card.name} создана. Сбор сведений запущен.`
      : null;
  }

  function getEditStatus(field: string) {
    const normalized = field.toLowerCase();
    if (normalized.includes("phone") || normalized.includes("телефон")) return "✅ Телефон изменён.";
    if (normalized.includes("email") || normalized.includes("почт")) return "✅ Рабочая почта изменена.";
    return "✅ Изменение сохранено.";
  }

  return (
    <>
      <button
        className={`chat-backdrop${open ? " chat-backdrop--visible" : ""}`}
        type="button"
        tabIndex={open ? 0 : -1}
        aria-label="Закрыть чат"
        aria-hidden={!open}
        onClick={onClose}
      />
      <aside
        className={`agent-chat${open ? " agent-chat--open" : ""}`}
        aria-label="Чат с ИИ-агентом"
        aria-modal={open ? true : undefined}
        role="dialog"
        aria-hidden={!open}
        inert={!open}
      >
        <header className="agent-chat__header">
          <div className="agent-chat__avatar" aria-hidden="true">AI</div>
          <div className="agent-chat__identity">
            <h2>ИИ-агент</h2>
            <p>{inCard ? universityName : "Общий чат"}</p>
          </div>
          <button className="icon-button agent-chat__close" type="button" onClick={onClose} aria-label="Закрыть чат">
            <Icon name="close" size={19} />
          </button>
        </header>
        <div className="agent-chat__modes" role="group" aria-label="Режим чата">
          <button type="button" aria-pressed={!inCard} disabled={typing}
            onClick={() => onModeChange("general")}>Общий чат</button>
          <button type="button" aria-pressed={inCard} disabled={typing || !cardId}
            onClick={() => onModeChange("card")}>По карточке</button>
        </div>

        <div className="agent-chat__conversation" aria-live="polite">
          {loading ? <p role="status">Загружаем историю…</p> : turns.length === 0 && !typing && visibleNotices.length === 0 ? (
            <div className="agent-chat__welcome">
              <h3>{inCard ? "Что узнать о вузе?" : "Чем могу помочь?"}</h3>
              <p>{inCard ? "Спросите о руководстве, контактах, образовательных направлениях, проектах или событиях."
                : "Спросите о реестре или работе с карточками. Чтобы собрать сведения о новом вузе, попросите создать карточку и укажите название; официальный сайт агент попробует найти сам."}</p>
            </div>
          ) : (
            <div className="agent-chat__messages">
              {turns.map((turn) => {
                const collectionStatus = getCollectionStatus(turn);
                return <div className="agent-chat__turn" key={turn.id}>
                  <div className="chat-message chat-message--user"><p>{turn.question}</p></div>
                  <div className="chat-message chat-message--assistant">
                    {open && latestRefresh?.answer.job_id && turn.id === latestRefresh.id
                      ? <CollectionProgress key={latestRefresh.answer.job_id} jobId={latestRefresh.answer.job_id}
                          initialText={turn.answer.text} />
                      : collectionStatus
                      ? <p className="chat-message__action-status chat-message__action-status--success" role="status">{collectionStatus}</p>
                      : <p>{turn.answer.text}</p>}
                    {turn.answer.cards && turn.answer.cards.length > 0 && <ul className="chat-message__cards">
                      {turn.answer.cards.map((card) => <li key={card.id}>
                        <button type="button" className="text-button" disabled={typing} onClick={() => onOpenCard(card)}>
                          {[card.name, card.city].filter(Boolean).join(" · ")}
                        </button>
                      </li>)}
                    </ul>}
                    {turn.answer.edit && <div className="chat-message__sources">
                      <p><strong>{turn.answer.edit.field}</strong> {turn.answer.edit.owner}</p>
                      {turn.answer.edit.status === "applied" && <p className="chat-message__action-status chat-message__action-status--success" role="status">
                        {getEditStatus(turn.answer.edit.field)}
                      </p>}
                      <p>Было: {turn.answer.edit.before}</p>
                      <p>Предлагается: {turn.answer.edit.after}</p>
                      {turn.answer.edit.status === "pending" && <button type="button" className="button button--soft"
                        disabled={typing || loading} onClick={() => { void confirmEdit(turn.id); }}>
                        Подтвердить правку
                      </button>}
                    </div>}
                    {turn.answer.sources.length > 0 && <details className="chat-message__sources">
                      <summary>Источники</summary>
                      <ul>{turn.answer.sources.map((source) => <li key={source.fact_id}>
                        {source.url ? <a href={source.url} target="_blank" rel="noreferrer">{source.value}</a> : source.value}
                        <p>{source.quote}</p>
                      </li>)}</ul>
                    </details>}
                  </div>
                </div>
              })}
              {pendingQuestion && <div className="chat-message chat-message--user"><p>{pendingQuestion}</p></div>}
              {visibleNotices.map((notice) => <div key={notice.id}
                className={`chat-message chat-message--status chat-message--status--${notice.tone}`} role="status">
                <p>{notice.message}</p>
                {notice.needsRecheck && <button type="button" className="button button--soft" onClick={onShowRecheck}>
                  Перейти к сверке сведений
                </button>}
              </div>)}
              {typing && (
                <div className="chat-message chat-message--assistant chat-message--typing" role="status" aria-live="polite">
                  <span /><span /><span />
                  <span className="chat-message__typing-label">{typingLabel}</span>
                </div>
              )}
              <div ref={messageEndRef} />
            </div>
          )}
        </div>

        <div className="agent-chat__footer">
          {error && <p className="research-error" role="alert">{error}</p>}
          <div className="agent-chat__quick-actions">
            {inCard && onAction && <>
              <button type="button" disabled={typing} onClick={() => { void runAction("presentation"); }}>Презентация</button>
              <button type="button" disabled={typing} onClick={() => { void runAction("refresh"); }}>Обновить данные</button>
            </>}
          </div>
          <form className="agent-chat__composer" onSubmit={sendMessage}>
            <input
              ref={inputRef}
              value={draft}
              onChange={(event) => { requestKey.current = null; setDraft(event.target.value); }}
              maxLength={2000}
              disabled={loading || typing}
              placeholder="Напишите сообщение..."
              aria-label="Сообщение агенту"
            />
            <button className="agent-chat__send" type="submit" disabled={!draft.trim() || typing || loading} aria-label="Отправить сообщение">
              <Icon name="send" size={19} />
            </button>
          </form>
        </div>
      </aside>
    </>
  );
}
