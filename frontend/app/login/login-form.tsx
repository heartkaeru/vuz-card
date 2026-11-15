"use client";

import { useState, type FormEvent } from "react";
import styles from "./login.module.css";

type LoginResult = { detail?: string };

function safeDestination(value: string | null) {
  if (!value) return "/";
  try {
    const destination = new URL(value, window.location.origin);
    if (destination.origin !== window.location.origin) return "/";
    return `${destination.pathname}${destination.search}${destination.hash}`;
  } catch {
    return "/";
  }
}

export default function LoginForm() {
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [registering, setRegistering] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setSubmitting(true);

    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch(`/api/auth/${registering ? "register" : "login"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: form.get("email"), password: form.get("password") }),
        cache: "no-store",
      });
      const result = await response.json().catch(() => ({})) as LoginResult;
      if (!response.ok) {
        setError(result.detail ?? "Не удалось выполнить вход. Проверьте логин и пароль.");
        return;
      }

      const next = new URLSearchParams(window.location.search).get("next");
      window.location.replace(safeDestination(next));
    } catch {
      setError("Не удалось подключиться к серверу. Попробуйте ещё раз.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className={styles.page}>
      <section className={styles.card} aria-labelledby="login-title">
        <div className={styles.brand} aria-label="Карточка ВУЗа">
          <span className={styles.brandMark} aria-hidden="true"><span /></span>
          <span>Карточка ВУЗа</span>
        </div>
        <div className={styles.heading}>
          <h1 id="login-title">{registering ? "Регистрация" : "Вход в систему"}</h1>
          <p className={styles.subtitle}>{registering ? "Создайте аккаунт, чтобы продолжить." : "Введите логин и пароль, чтобы продолжить."}</p>
        </div>

        <form className={styles.form} onSubmit={submit}>
          <label htmlFor="email">{registering ? "Email" : "Логин"}</label>
          <input
            id="email"
            name="email"
            type="email"
            autoComplete="username"
            placeholder="name@example.com"
            required
            autoFocus
          />

          <label htmlFor="password">Пароль</label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete={registering ? "new-password" : "current-password"}
            minLength={5}
            maxLength={72}
            required
          />

          {error && <p className={styles.error} role="alert">{error}</p>}

          <button className={styles.submit} type="submit" disabled={submitting}>
            {submitting ? (registering ? "Создаём аккаунт…" : "Входим…") : (registering ? "Зарегистрироваться" : "Войти")}
          </button>
        </form>
        <p className={styles.switch}>
          {registering ? "Уже есть аккаунт?" : "Нет аккаунта?"}
          <button
            type="button"
            disabled={submitting}
            onClick={() => { setRegistering(!registering); setError(""); }}
          >
            {registering ? "Войти" : "Зарегистрироваться"}
          </button>
        </p>
      </section>
    </main>
  );
}
