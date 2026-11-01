/**
 * PC hands: the desktop side of the `use_computer` tool.
 *
 * The agent loop on the server hands a goal to the desktop. This loop takes a
 * screenshot, asks the server for the next step (vision model), shows a
 * confirmation card when the step changes something, performs it on the PC, and
 * repeats until the model says done or fail, the user rejects, or Stop is pressed.
 */

export type ComputerRisk = "read" | "write" | "danger";

export interface ComputerAction {
  type: "click" | "double_click" | "right_click" | "type" | "key" | "scroll" | "wait" | "done" | "fail";
  /** 0..1000 grid over the screenshot. */
  x?: number;
  y?: number;
  text?: string;
  keys?: string;
  amount?: number;
  seconds?: number;
  target?: string;
  summary?: string;
}

export interface ComputerStep {
  stepId: string;
  action: ComputerAction;
  thought: string;
  risk: ComputerRisk;
  finished: boolean;
  error: string | null;
}

export interface ComputerShot {
  pngBase64: string;
  width: number;
  height: number;
}

export interface HistoryItem {
  action: string;
  target?: string;
  ok: boolean;
  note?: string;
}

export interface ConfirmRequest {
  goal: string;
  step: number;
  action: ComputerAction;
  thought: string;
  risk: ComputerRisk;
  shot: ComputerShot;
}

/** "all" approves ordinary steps until the goal is done; dangerous ones still ask. */
export type ConfirmAnswer = "approve" | "all" | "reject";

export interface ComputerPorts {
  shot(): Promise<ComputerShot>;
  step(request: { goal: string; screenshotBase64: string; history: HistoryItem[]; step: number }): Promise<ComputerStep>;
  decide(action: ComputerAction, flagged: boolean): Promise<"allow" | "confirm" | "deny">;
  confirm(request: ConfirmRequest): Promise<ConfirmAnswer>;
  act(action: ComputerAction, flagged: boolean, approved: boolean): Promise<void>;
  /** Log the user's answer in the trajectory. Optional, failures are ignored. */
  logConfirmation?(step: ComputerStep, answer: ConfirmAnswer): Promise<void>;
  /** Called around screenshots and actions so the Heli window is out of the way. */
  hideSelf?(): Promise<void>;
  showSelf?(): Promise<void>;
  stopped(): boolean;
  sleep?(ms: number): Promise<void>;
}

export interface ComputerOutcome {
  /** Short text for the agent loop's tool result. */
  content: string;
  error?: "stopped" | "rejected" | "needs_human" | "step_limit" | "failed";
  steps: number;
}

export const MAX_STEPS = 30;
const SETTLE_MS = 600;
const MAX_DENIED = 3;

export async function runUseComputer(goal: string, ports: ComputerPorts, maxSteps = MAX_STEPS): Promise<ComputerOutcome> {
  const history: HistoryItem[] = [];
  const sleep = ports.sleep ?? ((ms: number) => new Promise<void>((done) => setTimeout(done, ms)));
  let approveAll = false;
  let denied = 0;
  const done = (lines: string[]) => lines.filter(Boolean).join("\n");
  const trail = () => done(history.map((h, i) => `${i + 1}. ${describe({ type: h.action as ComputerAction["type"], target: h.target })}${h.ok ? "" : " — не вышло"}`));

  for (let index = 1; index <= maxSteps; index += 1) {
    if (ports.stopped()) {
      return { content: done(["Остановлено пользователем.", trail()]), error: "stopped", steps: index - 1 };
    }
    await ports.hideSelf?.();
    let shot: ComputerShot;
    try {
      shot = await ports.shot();
    } catch (err) {
      await ports.showSelf?.();
      if (String(err).includes("stopped")) {
        return { content: done(["Остановлено пользователем.", trail()]), error: "stopped", steps: index - 1 };
      }
      return { content: "Не удалось сделать снимок экрана.", error: "failed", steps: index - 1 };
    }
    let step: ComputerStep;
    try {
      step = await ports.step({ goal, screenshotBase64: shot.pngBase64, history, step: index });
    } catch (err) {
      await ports.showSelf?.();
      const message = err instanceof Error ? err.message : String(err);
      return { content: done([`Сервер не ответил: ${message}`, trail()]), error: "failed", steps: index - 1 };
    }
    const { action } = step;
    if (action.type === "done") {
      await ports.showSelf?.();
      return { content: done([`Готово: ${action.summary ?? goal}`, trail()]), steps: index };
    }
    if (action.type === "fail") {
      await ports.showSelf?.();
      return {
        content: done([`Не получилось без человека: ${action.summary ?? "нужна помощь"}`, trail()]),
        error: "needs_human",
        steps: index,
      };
    }
    if (step.error) {
      history.push({ action: action.type, ok: false, note: "ответ модели не разобран" });
      await sleep(SETTLE_MS);
      continue;
    }
    const flagged = step.risk === "danger";
    const decision = await ports.decide(action, flagged);
    if (decision === "deny") {
      if (ports.stopped()) {
        await ports.showSelf?.();
        return { content: done(["Остановлено пользователем.", trail()]), error: "stopped", steps: index };
      }
      denied += 1;
      history.push({ action: action.type, target: action.target, ok: false, note: "запрещено правилами Heli" });
      if (denied >= MAX_DENIED) {
        await ports.showSelf?.();
        return { content: done(["Правила Heli запрещают нужные шаги.", trail()]), error: "failed", steps: index };
      }
      continue;
    }
    let approved = decision === "allow";
    if (decision === "confirm" && approveAll && step.risk !== "danger") approved = true;
    if (!approved) {
      await ports.showSelf?.();
      const answer = await ports.confirm({ goal, step: index, action, thought: step.thought, risk: step.risk, shot });
      void ports.logConfirmation?.(step, answer).catch(() => undefined);
      if (answer === "reject") {
        return {
          content: done([`Пользователь отклонил шаг: ${describe(action)}.`, trail()]),
          error: "rejected",
          steps: index,
        };
      }
      if (answer === "all") approveAll = true;
      approved = true;
      if (ports.stopped()) {
        return { content: done(["Остановлено пользователем.", trail()]), error: "stopped", steps: index };
      }
      await ports.hideSelf?.();
    }
    try {
      await ports.act(action, flagged, approved);
      history.push({ action: action.type, target: action.target, ok: true });
    } catch (err) {
      const message = String(err instanceof Error ? err.message : err);
      if (message.includes("stopped")) {
        await ports.showSelf?.();
        return { content: done(["Остановлено пользователем.", trail()]), error: "stopped", steps: index };
      }
      history.push({ action: action.type, target: action.target, ok: false, note: message.slice(0, 200) });
    }
    await sleep(SETTLE_MS);
  }
  await ports.showSelf?.();
  return { content: done([`Достигнут предел в ${maxSteps} шагов, цель не подтверждена.`, trail()]), error: "step_limit", steps: maxSteps };
}

/** Human wording for the confirmation card and the step log. */
export function describe(action: Pick<ComputerAction, "type" | "target"> & Partial<ComputerAction>): string {
  const target = action.target?.trim();
  const where = target ? (target.includes("«") ? ` ${target}` : ` «${target}»`) : "";
  switch (action.type) {
    case "click":
      return `нажать${where}`;
    case "double_click":
      return `дважды нажать${where}`;
    case "right_click":
      return `открыть меню${where}`;
    case "type":
      return action.text ? `ввести «${shorten(action.text, 60)}»${action.target ? ` в${where}` : ""}` : `ввести текст${where}`;
    case "key":
      return action.keys ? `нажать ${prettyKeys(action.keys)}` : "нажать клавиши";
    case "scroll":
      return (action.amount ?? 0) < 0 ? "прокрутить вверх" : "прокрутить вниз";
    case "wait":
      return "подождать";
    case "done":
      return "закончить";
    case "fail":
      return "остановиться";
    default:
      return "действие";
  }
}

export function prettyKeys(keys: string): string {
  const names: Record<string, string> = {
    ctrl: "Ctrl",
    control: "Ctrl",
    alt: "Alt",
    shift: "Shift",
    win: "Win",
    meta: "Win",
    enter: "Enter",
    return: "Enter",
    esc: "Esc",
    escape: "Esc",
    tab: "Tab",
    delete: "Delete",
    del: "Delete",
    backspace: "Backspace",
    space: "Пробел",
  };
  return keys
    .split("+")
    .map((part) => names[part] ?? (part.length === 1 ? part.toUpperCase() : part.toUpperCase()))
    .join("+");
}

function shorten(text: string, limit: number): string {
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

/** Where to draw the marker on the screenshot preview, in percent. */
export function markerPosition(action: ComputerAction): { left: string; top: string } | null {
  if (action.x === undefined || action.y === undefined) return null;
  const clamp = (v: number) => Math.min(1000, Math.max(0, v)) / 10;
  return { left: `${clamp(action.x)}%`, top: `${clamp(action.y)}%` };
}

/** Converts the server JSON (snake_case) into a ComputerStep. */
export function parseStep(raw: unknown): ComputerStep {
  const body = (raw ?? {}) as Record<string, unknown>;
  const action = (body.action ?? {}) as ComputerAction;
  const risk = body.risk === "read" || body.risk === "danger" ? body.risk : "write";
  return {
    stepId: String(body.step_id ?? ""),
    action,
    thought: typeof body.thought === "string" ? body.thought : "",
    risk,
    finished: Boolean(body.finished),
    error: typeof body.error === "string" ? body.error : null,
  };
}
