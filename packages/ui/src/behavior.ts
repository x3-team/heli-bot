export type LimitTone = "ok" | "warn" | "stop";

export function limitTone(percent: number): LimitTone {
  if (percent >= 100) return "stop";
  if (percent >= 80) return "warn";
  return "ok";
}

export function clampPercent(percent: number): number {
  if (!Number.isFinite(percent) || percent < 0) return 0;
  if (percent > 100) return 100;
  return Math.round(percent);
}

const DANGER = /(удал|оплат|перевод|отправ|письм|парол|сообщен)/i;

export function confirmationNeeded(text: string): boolean {
  return DANGER.test(text);
}

/** «Компьютер агента» panel width: at least 300px, and the chat keeps at least 360px. */
export const COMPUTER_MIN_WIDTH = 300;
const CHAT_MIN_WIDTH = 360;

/** sideWidth: the left menu, 248px with labels or 72px icons only. */
export function clampComputerWidth(width: number, sideWidth: number, viewport: number): number {
  const max = viewport - sideWidth - CHAT_MIN_WIDTH;
  return Math.round(Math.max(COMPUTER_MIN_WIDTH, Math.min(width, Math.max(COMPUTER_MIN_WIDTH, max))));
}
