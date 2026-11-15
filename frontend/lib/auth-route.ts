import { NextResponse } from "next/server";

const ACCESS_TOKEN_COOKIE = "vuz_access_token";

type AuthResponse = {
  access_token: string;
  expires_in: number;
  detail?: string;
};

type ValidationIssue = {
  loc?: unknown[];
  type?: string;
  msg?: string;
};

function validationMessage(detail: unknown) {
  if (!Array.isArray(detail)) return null;

  const issues = detail as ValidationIssue[];
  const missing = new Set<string>();
  let invalidEmail = false;
  let shortPassword = false;
  let longPassword = false;
  let oversizedPassword = false;

  for (const issue of issues) {
    const field = issue.loc?.at(-1);
    if (issue.type === "missing" && typeof field === "string") missing.add(field);
    if (field === "email") invalidEmail = true;
    if (field === "password") {
      if (issue.type?.includes("too_short")) shortPassword = true;
      if (issue.type?.includes("too_long")) longPassword = true;
      if (issue.msg?.includes("72 байт")) oversizedPassword = true;
    }
  }

  if (missing.size) {
    const labels: Record<string, string> = {
      email: "email",
      password: "пароль",
      first_name: "имя",
      last_name: "фамилию",
    };
    const fields = [...missing].map((field) => labels[field] ?? field);
    return `Заполните обязательные поля: ${fields.join(", ")}.`;
  }
  if (oversizedPassword) return "Пароль должен занимать не более 72 байт.";
  if (shortPassword) return "Пароль должен содержать не менее 5 символов.";
  if (longPassword) return "Пароль не должен превышать 72 символа.";
  if (invalidEmail) return "Проверьте формат email.";
  return "Проверьте введённые данные.";
}

export async function handleAuth(request: Request, action: "login" | "register") {
  let credentials: unknown;
  try {
    credentials = await request.json();
  } catch {
    return NextResponse.json({ detail: "Введите логин и пароль." }, { status: 400 });
  }

  const backendUrl = (process.env.VUZ_BACKEND_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
  let backendResponse: Response;
  try {
    backendResponse = await fetch(`${backendUrl}/api/v1/auth/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(credentials),
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { detail: "Не удалось подключиться к серверу. Попробуйте ещё раз." },
      { status: 503 },
    );
  }

  const result = await backendResponse.json().catch(() => ({})) as Partial<AuthResponse>;
  if (!backendResponse.ok) {
    const detail = action === "login" && backendResponse.status === 401
      ? "Неверный логин или пароль."
      : typeof result.detail === "string"
        ? result.detail
        : validationMessage(result.detail) ?? "Не удалось выполнить запрос. Проверьте данные и повторите.";
    return NextResponse.json({ detail }, { status: backendResponse.status });
  }
  if (!result.access_token || !result.expires_in) {
    return NextResponse.json({ detail: "Сервер вернул некорректный ответ." }, { status: 502 });
  }

  const response = NextResponse.json({ ok: true }, { status: backendResponse.status });
  response.cookies.set(ACCESS_TOKEN_COOKIE, result.access_token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: result.expires_in,
  });
  return response;
}
