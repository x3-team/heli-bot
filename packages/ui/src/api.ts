export interface Usage {
  weekPercent: number;
  monthPercent: number;
  blocked: boolean;
  warning: boolean;
}

export interface SessionUser {
  consentPdn: boolean;
  consentTraining: boolean;
}

const ERRORS: Record<string, string> = {
  email_not_configured: "Почта для кода ещё не подключена. Код выдаёт оператор.",
  invite_invalid: "Код приглашения не подошёл.",
  code_invalid: "Код не подошёл.",
  token_invalid: "Нужно войти снова.",
  pdn_consent_required: "Нужно согласие на обработку данных.",
  missing_api_key: "На сервере не задан ключ модели.",
  request_failed: "Модель не ответила. Попробуйте ещё раз.",
  unknown_alias: "Сервер не принял шаг.",
  limit_reached: "Лимит исчерпан",
};

export class ApiError extends Error {
  constructor(message: string) {
    super(message);
  }
}

async function readError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    if (body.detail && ERRORS[body.detail]) return ERRORS[body.detail];
  } catch {
    // ignore a non-JSON body
  }
  return "Не получилось. Попробуйте ещё раз.";
}

async function send(base: string, path: string, init: RequestInit): Promise<Response> {
  return fetch(`${base}${path}`, init);
}

export async function requestCode(base: string, email: string, inviteCode: string): Promise<void> {
  const response = await send(base, "/v1/auth/request-code", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, invite_code: inviteCode }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
}

export async function verifyCode(base: string, email: string, code: string): Promise<string> {
  const response = await send(base, "/v1/auth/verify", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, code }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
  const body = (await response.json()) as { access_token: string };
  return body.access_token;
}

export async function saveConsents(
  base: string,
  token: string,
  consentPdn: boolean,
  consentTraining: boolean,
): Promise<void> {
  const response = await send(base, "/v1/me/consents", {
    method: "PUT",
    headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
    body: JSON.stringify({ consent_pdn: consentPdn, consent_training: consentTraining }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
}

export async function loadMe(base: string, token: string): Promise<SessionUser> {
  const response = await send(base, "/v1/me", {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new ApiError(await readError(response));
  const body = (await response.json()) as { consent_pdn: boolean; consent_training: boolean };
  return { consentPdn: body.consent_pdn, consentTraining: body.consent_training };
}

export async function loadUsage(base: string, token: string): Promise<Usage> {
  const response = await send(base, "/v1/usage", {
    headers: { authorization: `Bearer ${token}` },
  });
  if (response.status === 404) {
    return { weekPercent: 0, monthPercent: 0, blocked: false, warning: false };
  }
  if (!response.ok) throw new ApiError(await readError(response));
  const body = (await response.json()) as {
    week_percent?: number;
    month_percent?: number;
    blocked?: boolean;
    warning?: boolean;
  };
  return {
    weekPercent: body.week_percent ?? 0,
    monthPercent: body.month_percent ?? 0,
    blocked: Boolean(body.blocked),
    warning: Boolean(body.warning),
  };
}

export async function runTask(
  base: string,
  token: string,
  text: string,
  signal: AbortSignal,
): Promise<string> {
  const created = await send(base, "/v1/tasks", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
    body: JSON.stringify({ request: text, context: {} }),
    signal,
  });
  if (!created.ok) throw new ApiError(await readError(created));
  const task = (await created.json()) as { id: string };
  const run = await send(base, `/v1/tasks/${task.id}/run`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}` },
    signal,
  });
  if (!run.ok) throw new ApiError(await readError(run));
  const body = (await run.json()) as { reply?: string };
  await send(base, `/v1/tasks/${task.id}/finish`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
    body: JSON.stringify({ outcome: "success", final_answer: body.reply ?? "" }),
    signal,
  });
  return body.reply ?? "";
}
