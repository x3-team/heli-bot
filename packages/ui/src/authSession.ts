export interface AuthResult {
  accessToken: string;
  trialActive: boolean;
}

export function parseAuthResponse(body: {
  access_token: string;
  trial_active?: boolean;
}): AuthResult {
  return {
    accessToken: body.access_token,
    trialActive: Boolean(body.trial_active),
  };
}

/** Sidebar and limits label, e.g. «Пробная неделя до 8 ноября» (date in Moscow time). */
export function trialLabel(endsAt?: string | null): string {
  if (!endsAt) return "Пробная неделя";
  const date = new Date(endsAt);
  if (Number.isNaN(date.getTime())) return "Пробная неделя";
  const day = new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "long",
    timeZone: "Europe/Moscow",
  }).format(date);
  return `Пробная неделя до ${day}`;
}
