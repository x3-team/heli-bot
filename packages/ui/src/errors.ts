const ERRORS: Record<string, string> = {
  auth_not_configured: "На сервере не настроена авторизация (JWT_SECRET).",
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
  hash_pepper_missing: "На сервере не настроен TRAJECTORY_HASH_PEPPER.",
  browser_unavailable: "Браузер на сервере сейчас недоступен.",
  browser_busy: "Свободных браузеров нет. Попробуйте позже.",
  vault_unavailable: "Хранилище доступов на сервере не настроено.",
  bad_schedule: "Нужна фраза «каждый понедельник в 9:00» или пять полей cron.",
  local_browser_required: "Нужен локальный Chrome или Edge на этом компьютере.",
  bad_browser: "Поддерживаются только Chrome и Edge.",
  cloud_running: "Сначала остановите облачный браузер.",
};

export function mapErrorCode(code: string): string {
  return ERRORS[code] ?? code;
}

export async function readApiError(response: Response): Promise<string> {
  const statusHint =
    response.status === 404
      ? "Сервис не найден (404). Проверьте адрес API."
      : response.status >= 500
        ? "Сервер недоступен. Попробуйте позже."
        : `Ошибка ${response.status}`;

  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body.detail;
    if (typeof detail === "string") {
      return mapErrorCode(detail);
    }
    if (Array.isArray(detail)) {
      const parts = detail.map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          return String((item as { msg: string }).msg);
        }
        return "Неверные данные формы.";
      });
      return parts.filter(Boolean).join(" ") || statusHint;
    }
  } catch {
    // not JSON
  }
  return statusHint;
}

export function networkErrorMessage(): string {
  return "Нет связи с сервером. Проверьте интернет и адрес API.";
}
