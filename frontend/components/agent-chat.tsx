"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Icon } from "@/components/icon";
import { api, errorText, type ApiCard, type Job, type CardActionResult } from "@/lib/api";

type Turn = {
  id: number; question: string;
  answer: {
    text: string; sources: { fact_id: string; value: string; quote: string; url: string | null }[];
    cards?: ApiCard[];
    edit?: { field: string; owner: string; before: string; after: string; status: "pending" | "applied" };
  };
};
const TOKEN_KEY = "vuz-crm-chat-token";

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
  onAction?: (action: "refresh" | "presentation") => Promise<CardActionResult>;
  onShowRecheck?: () => void;
};

export function AgentChat({ open, universityName, cardId, mode, onModeChange, onOpenCard, onClose, onAction,
  onEditApplied, onCollection, onShowRecheck }: AgentChatProps) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [typing, setTyping] = useState(false);
  const [loading, setLoading] = useState(false);
  const [token, setToken] = useState("");
  const [register, setRegister] = useState(false);
  const [error, setError] = useState("");
  const [actionStatus, setActionStatus] = useState("");
  const [needsRecheck, setNeedsRecheck] = useState(false);
  const requestKey = useRef<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const messageEndRef = useRef<HTMLDivElement>(null);
  const inCard = mode === "card";
  const path = inCard && cardId ? `/cards/${encodeURIComponent(cardId)}/chat` : "/chat/general";

  useEffect(() => { setToken(sessionStorage.getItem(TOKEN_KEY) ?? ""); }, []);
  useEffect(() => { if (!open) { setActionStatus(""); setNeedsRecheck(false); } }, [open]);
  useEffect(() => {
    if (!open || !token) return;
    const controller = new AbortController();
    setLoading(true);
    api<Turn[]>(path, { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal })
      .then((history) => { if (!controller.signal.aborted) setTurns(history); })
      .catch((failure) => { if (!controller.signal.aborted) showError(failure); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [open, token, path]);

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
  }, [open, onClose, token, loading]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, typing]);

  function logout() {
    sessionStorage.removeItem(TOKEN_KEY);
    setToken(""); setTurns([]); setLoading(false); setDraft(""); setActionStatus("");
  }

  function showError(failure: unknown) {
    if (failure instanceof Error && failure.message === "chat_login_required") logout();
    setError(errorText(failure));
  }

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setTyping(true); setError(""); setActionStatus("");
    try {
      const response = await fetch(`/api/v1/auth/${register ? "register" : "login"}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: form.get("email"), password: form.get("password") }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Проверьте email, пароль и настройки авторизации.");
      sessionStorage.setItem(TOKEN_KEY, result.access_token);
      setLoading(true); setToken(result.access_token);
    } catch (failure) { showError(failure); }
    finally { setTyping(false); }
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || typing || loading || !token) return;
    setTyping(true); setError(""); setActionStatus("");
    const headers = { Authorization: `Bearer ${token}` };
    try {
      const request_key = requestKey.current ??= crypto.randomUUID();
      const result = await api<Turn | { collection: { card: ApiCard; job: Job } }>(
        path, { method: "POST", headers, body: JSON.stringify({ message, request_key }) },
      );
      if ("collection" in result) onCollection(result.collection.card, result.collection.job);
      else setTurns((current) => current.some((saved) => saved.id === result.id) ? current : [...current, result].slice(-20));
      requestKey.current = null;
      setDraft("");
    } catch (failure) {
      showError(failure);
      if (failure instanceof Error && failure.message === "chat_history_changed") {
        try { setTurns(await api<Turn[]>(path, { headers })); }
        catch (historyFailure) { showError(historyFailure); }
      }
    } finally { setTyping(false); }
  }

  async function runAction(action: "refresh" | "presentation") {
    if (!onAction || typing) return;
    setTyping(true); setActionStatus(""); setNeedsRecheck(false); setError("");
    try {
      const result = await onAction(action);
      setActionStatus(result.message);
      setNeedsRecheck(Boolean(result.needsRecheck));
    }
    catch (failure) { showError(failure); }
    finally { setTyping(false); }
  }

  async function confirmEdit(turnId: number) {
    if (typing || loading || !token || !inCard || !cardId) return;
    setTyping(true); setError("");
    const headers = { Authorization: `Bearer ${token}` };
    try {
      await api(`${path}/${turnId}/confirm`, { method: "POST", headers });
      onEditApplied?.();
      setTurns(await api<Turn[]>(path, { headers }));
    } catch (failure) { showError(failure); }
    finally { setTyping(false); }
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
            <p>{inCard ? universityName : "ВУЗ-CRM"}</p>
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
          {!token ? (
            <form className="modal-form" onSubmit={signIn}>
              <h3>{register ? "Регистрация" : "Вход в чат"}</h3>
              <label>Email<input name="email" type="email" autoComplete="username" required /></label>
              <label>Пароль<input name="password" type="password" required minLength={5} maxLength={72}
                autoComplete={register ? "new-password" : "current-password"} /></label>
              <button className="button button--primary" disabled={typing} type="submit">
                {register ? "Зарегистрироваться" : "Войти"}
              </button>
              <button className="text-button" type="button" disabled={typing}
                onClick={() => { setRegister(!register); setError(""); }}>
                {register ? "Уже есть аккаунт" : "Создать аккаунт"}
              </button>
            </form>
          ) : loading ? <p role="status">Загружаем историю…</p> : turns.length === 0 && !typing ? (
            <div className="agent-chat__welcome">
              <h3>{inCard ? "Что узнать о вузе?" : "Чем помочь в ВУЗ-CRM?"}</h3>
              <p>{inCard ? "Спросите о руководстве, контактах, образовательных направлениях, проектах или событиях."
                : "Спросите о реестре или работе с карточками. Для сбора по новому вузу укажите название и официальный URL."}</p>
            </div>
          ) : (
            <div className="agent-chat__messages">
              {turns.map((turn) => (
                <div className="agent-chat__turn" key={turn.id}>
                  <div className="chat-message chat-message--user"><p>{turn.question}</p></div>
                  <div className="chat-message chat-message--assistant">
                    <p>{turn.answer.text}</p>
                    {turn.answer.cards && turn.answer.cards.length > 0 && <ul className="chat-message__cards">
                      {turn.answer.cards.map((card) => <li key={card.id}>
                        <button type="button" className="text-button" disabled={typing} onClick={() => onOpenCard(card)}>
                          {[card.name, card.city].filter(Boolean).join(" · ")}
                        </button>
                      </li>)}
                    </ul>}
                    {turn.answer.edit && <div className="chat-message__sources">
                      <p><strong>{turn.answer.edit.field}</strong> {turn.answer.edit.owner}</p>
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
              ))}
              {typing && (
                <div className="chat-message chat-message--assistant chat-message--typing" aria-label="Агент печатает">
                  <span /><span /><span />
                </div>
              )}
              <div ref={messageEndRef} />
            </div>
          )}
        </div>

        <div className="agent-chat__footer">
          {error && <p className="research-error" role="alert">{error}</p>}
          {actionStatus && <p role="status">{actionStatus}</p>}
          {actionStatus && needsRecheck && <button type="button" className="button button--soft" onClick={onShowRecheck}>
            Перейти к сверке сведений
          </button>}
          <div className="agent-chat__quick-actions">
            {inCard && onAction && <>
              <button type="button" disabled={typing} onClick={() => { void runAction("presentation"); }}>Презентация</button>
              <button type="button" disabled={typing} onClick={() => { void runAction("refresh"); }}>Обновить данные</button>
            </>}
            {token && <button type="button" disabled={typing} onClick={() => { logout(); setError(""); }}>Выйти</button>}
          </div>
          {token && <form className="agent-chat__composer" onSubmit={sendMessage}>
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
          </form>}
        </div>
      </aside>
    </>
  );
}
