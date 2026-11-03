"use client";

import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Icon } from "@/components/icon";

type Message = { id: number; role: "user" | "assistant"; text: string };
type AgentChatProps = {
  open: boolean;
  universityName: string;
  onClose: () => void;
};

export function AgentChat({ open, universityName, onClose }: AgentChatProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [typing, setTyping] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const messageEndRef = useRef<HTMLDivElement>(null);
  const nextId = useRef(0);

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
  }, [open, onClose]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, typing]);

  const sendMessage = (text: string) => {
    const value = text.trim();
    if (!value || typing) return;
    const requestId = nextId.current++;
    setMessages((current) => [...current, { id: requestId, role: "user", text: value }]);
    setDraft("");
    setTyping(true);

    window.setTimeout(() => {
      const action = value.toLocaleLowerCase("ru-RU");
      const response = action.includes("обновить")
        ? `Запрос на обновление карточки «${universityName}» подготовлен. В этой версии интерфейс не подключён к источникам данных, поэтому сбор не запускался.`
        : action.includes("контакт")
          ? `Чтобы добавить контакт в карточку «${universityName}», сначала нужны его данные и подтверждающий источник. Здесь можно подключить этот сценарий к серверу.`
          : `Запрос по «${universityName}» принят в демо-интерфейсе. Сейчас карточка не подключена к поиску и CRM: перед добавлением сведений понадобится загрузить источники и проверить факты.`;
      const responseId = nextId.current++;
      setMessages((current) => [...current, { id: responseId, role: "assistant", text: response }]);
      setTyping(false);
    }, 620);
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    sendMessage(draft);
  };

  const handleInputKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage(draft);
    }
  };

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
            <p><span className="status-dot" /> Онлайн · готов помочь</p>
          </div>
          <button className="icon-button agent-chat__close" type="button" onClick={onClose} aria-label="Закрыть чат">
            <Icon name="close" size={19} />
          </button>
        </header>

        <div className="agent-chat__conversation" aria-live="polite">
          {messages.length === 0 ? (
            <div className="agent-chat__welcome">
              <h3>Чем могу помочь?</h3>
              <p>Найду информацию по вузам, обновлю отчёт или помогу найти контакты.</p>
            </div>
          ) : (
            <div className="agent-chat__messages">
              {messages.map((message) => (
                <div className={`chat-message chat-message--${message.role}`} key={message.id}>
                  <p>{message.text}</p>
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
          <div className="agent-chat__quick-actions">
            <button type="button" onClick={() => sendMessage("Добавить контакт")}>Добавить контакт</button>
            <button type="button" onClick={() => sendMessage("Обновить данные")}>Обновить данные</button>
          </div>
          <form className="agent-chat__composer" onSubmit={handleSubmit}>
            <button
              className="agent-chat__attach"
              type="button"
              title="Вложение будет доступно после подключения сервиса"
              aria-label="Добавить вложение"
            >
              <Icon name="plus" size={18} />
            </button>
            <input
              ref={inputRef}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={handleInputKeyDown}
              placeholder="Напишите сообщение..."
              aria-label="Сообщение агенту"
            />
            <button className="agent-chat__send" type="submit" disabled={!draft.trim() || typing} aria-label="Отправить сообщение">
              <Icon name="send" size={19} />
            </button>
          </form>
          <p className="agent-chat__note">Демо-интерфейс · без подключения к источникам</p>
        </div>
      </aside>
    </>
  );
}
