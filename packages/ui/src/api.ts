export interface Usage {
  weekPercent: number;
  monthPercent: number;
  blocked: boolean;
  warning: boolean;
  trialActive: boolean;
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
  email_taken: "Этот адрес уже зарегистрирован.",
  credentials_invalid: "Почта или пароль не подошли.",
  password_weak: "Пароль должен быть не короче 8 символов.",
  user_not_found: "Пользователь не найден.",
  browser_unavailable: "Браузер на сервере сейчас недоступен.",
  browser_busy: "Свободных браузеров нет. Попробуйте позже.",
  vault_unavailable: "Хранилище доступов на сервере не настроено.",
  bad_schedule: "Нужна фраза «каждый понедельник в 9:00» или пять полей cron.",
  local_browser_required: "Нужен локальный Chrome или Edge на этом компьютере.",
  bad_browser: "Поддерживаются только Chrome и Edge.",
  cloud_running: "Сначала остановите облачный браузер.",
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

export async function requestCode(base: string, email: string, inviteCode?: string): Promise<void> {
  const payload: { email: string; invite_code?: string } = { email };
  if (inviteCode?.trim()) payload.invite_code = inviteCode.trim();
  const response = await send(base, "/v1/auth/request-code", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new ApiError(await readError(response));
}

export async function registerAccount(
  base: string,
  email: string,
  password: string,
  consentPdn: boolean,
  consentTraining: boolean,
): Promise<string> {
  const response = await send(base, "/v1/auth/register", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      email,
      password,
      consent_pdn: consentPdn,
      consent_training: consentTraining,
    }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
  const body = (await response.json()) as { access_token: string };
  return body.access_token;
}

export async function loginPassword(base: string, email: string, password: string): Promise<string> {
  const response = await send(base, "/v1/auth/login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
  const body = (await response.json()) as { access_token: string };
  return body.access_token;
}

export async function recoverRequest(base: string, email: string): Promise<void> {
  const response = await send(base, "/v1/auth/recover/request", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
}

export async function recoverReset(
  base: string,
  email: string,
  code: string,
  password: string,
): Promise<string> {
  const response = await send(base, "/v1/auth/recover/reset", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, code, password }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
  const body = (await response.json()) as { access_token: string };
  return body.access_token;
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
    return { weekPercent: 0, monthPercent: 0, blocked: false, warning: false, trialActive: false };
  }
  if (!response.ok) throw new ApiError(await readError(response));
  const body = (await response.json()) as {
    week_percent?: number;
    month_percent?: number;
    blocked?: boolean;
    warning?: boolean;
    trial_active?: boolean;
  };
  return {
    weekPercent: body.week_percent ?? 0,
    monthPercent: body.month_percent ?? 0,
    blocked: Boolean(body.blocked),
    warning: Boolean(body.warning),
    trialActive: Boolean(body.trial_active),
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

export interface BrowserView {
  status: "running" | "stopped";
  mode: "local" | "cloud";
  novncUrl: string | null;
  localBrowser: string | null;
  localPort: number | null;
}

export async function loadBrowser(base: string, token: string): Promise<BrowserView> {
  const response = await send(base, "/v1/browser", {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new ApiError(await readError(response));
  return readBrowser(response);
}

export async function attachLocalBrowser(
  base: string,
  token: string,
  browser: "chrome" | "edge",
  port: number,
): Promise<BrowserView> {
  const response = await send(base, "/v1/browser/local/attach", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
    body: JSON.stringify({ browser, port }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
  return readBrowser(response);
}

export async function startBrowser(
  base: string,
  token: string,
  preferLocal = true,
): Promise<BrowserView> {
  const response = await send(base, `/v1/browser/start?prefer_local=${preferLocal ? "true" : "false"}`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new ApiError(await readError(response));
  return readBrowser(response);
}

export async function stopBrowser(base: string, token: string): Promise<BrowserView> {
  const response = await send(base, "/v1/browser/stop", {
    method: "POST",
    headers: { authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new ApiError(await readError(response));
  return readBrowser(response);
}

export interface CredentialItem {
  id: string;
  site: string;
  username: string;
}

export async function loadCredentials(base: string, token: string): Promise<CredentialItem[]> {
  const response = await send(base, "/v1/credentials", {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new ApiError(await readError(response));
  const body = (await response.json()) as { credentials?: CredentialItem[] };
  return body.credentials ?? [];
}

export async function saveCredential(
  base: string,
  token: string,
  site: string,
  username: string,
  secret: string,
): Promise<CredentialItem> {
  const response = await send(base, "/v1/credentials", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
    body: JSON.stringify({ site, username, secret }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
  return (await response.json()) as CredentialItem;
}

export interface ScheduleItem {
  id: string;
  label: string;
  expression: string;
  enabled: boolean;
  nextRun: string;
  lastResult: string | null;
  lastError: string | null;
}

export interface MemoryItem {
  id: string;
  text: string;
}

export async function loadMemory(base: string, token: string): Promise<MemoryItem[]> {
  const response = await send(base, "/v1/memory", {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new ApiError(await readError(response));
  const body = (await response.json()) as { facts?: MemoryItem[] };
  return body.facts ?? [];
}

export async function saveMemory(base: string, token: string, text: string): Promise<MemoryItem> {
  const response = await send(base, "/v1/memory", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
    body: JSON.stringify({ text }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
  return (await response.json()) as MemoryItem;
}

export async function deleteMemory(base: string, token: string, id: string): Promise<void> {
  const response = await send(base, `/v1/memory/${id}`, {
    method: "DELETE",
    headers: { authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new ApiError(await readError(response));
}

export async function loadSchedules(base: string, token: string): Promise<ScheduleItem[]> {
  const response = await send(base, "/v1/schedules", {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new ApiError(await readError(response));
  const body = (await response.json()) as { schedules?: Array<Record<string, unknown>> };
  return (body.schedules ?? []).map(readSchedule);
}

export async function saveSchedule(
  base: string,
  token: string,
  label: string,
  expression: string,
  request: string,
): Promise<ScheduleItem> {
  const response = await send(base, "/v1/schedules", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
    body: JSON.stringify({ label, expression, request }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
  return readSchedule((await response.json()) as Record<string, unknown>);
}

function readSchedule(body: Record<string, unknown>): ScheduleItem {
  return {
    id: String(body.id ?? ""),
    label: String(body.label ?? ""),
    expression: String(body.expression ?? ""),
    enabled: Boolean(body.enabled),
    nextRun: String(body.next_run ?? ""),
    lastResult: body.last_result ? String(body.last_result) : null,
    lastError: body.last_error ? String(body.last_error) : null,
  };
}

export async function deleteCredential(base: string, token: string, id: string): Promise<void> {
  const response = await send(base, `/v1/credentials/${id}`, {
    method: "DELETE",
    headers: { authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new ApiError(await readError(response));
}

async function readBrowser(response: Response): Promise<BrowserView> {
  const body = (await response.json()) as {
    status?: string;
    mode?: string;
    novnc_url?: string | null;
    local_browser?: string | null;
    local_port?: number | null;
  };
  return {
    status: body.status === "running" ? "running" : "stopped",
    mode: body.mode === "local" ? "local" : "cloud",
    novncUrl: body.novnc_url ?? null,
    localBrowser: body.local_browser ?? null,
    localPort: body.local_port ?? null,
  };
}
