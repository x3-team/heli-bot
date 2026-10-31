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
