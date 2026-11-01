/** Tauri and HTTP wiring for runUseComputer. Only works inside the desktop app. */

import { readApiError } from "./errors";
import {
  parseStep,
  runUseComputer,
  type ComputerAction,
  type ComputerOutcome,
  type ComputerPorts,
  type ComputerShot,
  type ConfirmAnswer,
  type ConfirmRequest,
} from "./computer";

type Listener = (request: ConfirmRequest | null) => void;

/**
 * One pending confirmation at a time. The loop asks, the card in the chat
 * answers. Kept outside React so the client-action dispatcher can use it.
 */
export class ConfirmQueue {
  private current: { request: ConfirmRequest; resolve: (answer: ConfirmAnswer) => void } | null = null;
  private listeners = new Set<Listener>();

  ask(request: ConfirmRequest): Promise<ConfirmAnswer> {
    this.current?.resolve("reject");
    return new Promise<ConfirmAnswer>((resolve) => {
      this.current = { request, resolve };
      this.emit();
    });
  }

  answer(answer: ConfirmAnswer): void {
    const current = this.current;
    this.current = null;
    this.emit();
    current?.resolve(answer);
  }

  /** Stop pressed: any open card counts as rejected. */
  cancel(): void {
    if (this.current) this.answer("reject");
  }

  pending(): ConfirmRequest | null {
    return this.current?.request ?? null;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    const request = this.pending();
    for (const listener of this.listeners) listener(request);
  }
}

export const computerConfirm = new ConfirmQueue();

export function isDesktop(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

async function invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke: call } = await import("@tauri-apps/api/core");
  return call<T>(command, args);
}

async function heliWindow() {
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  return getCurrentWindow();
}

export interface DesktopComputerOptions {
  base: string;
  token: string;
  taskId: string;
  /** Settings toggle "Спрашивать перед кликами". Dangerous steps ask anyway. */
  confirmWrites?: boolean;
  /** True once the user pressed Stop or the global pause hotkey. */
  isStopped: () => boolean;
  confirm?: (request: ConfirmRequest) => Promise<ConfirmAnswer>;
}

export function desktopComputerPorts(options: DesktopComputerOptions): ComputerPorts {
  const confirmWrites = options.confirmWrites ?? true;
  const headers = { "content-type": "application/json", authorization: `Bearer ${options.token}` };
  return {
    async shot() {
      const raw = await invoke<{ png_base64: string; width: number; height: number }>("computer_shot");
      const shot: ComputerShot = { pngBase64: raw.png_base64, width: raw.width, height: raw.height };
      return shot;
    },
    async step({ goal, screenshotBase64, history, step }) {
      const response = await fetch(`${options.base}/v1/tasks/${options.taskId}/computer/step`, {
        method: "POST",
        headers,
        body: JSON.stringify({ goal, screenshot_base64: screenshotBase64, history, step }),
      });
      if (!response.ok) throw new Error(await readApiError(response));
      return parseStep(await response.json());
    },
    decide(action: ComputerAction, flagged: boolean) {
      return invoke<"allow" | "confirm" | "deny">("computer_decide", {
        action,
        flagged,
        confirmWrites,
      });
    },
    confirm(request) {
      return (options.confirm ?? ((r) => computerConfirm.ask(r)))(request);
    },
    async act(action, flagged, approved) {
      await invoke<void>("computer_act", { action, flagged, confirmWrites, approved });
    },
    async logConfirmation(step, answer) {
      await fetch(`${options.base}/v1/tasks/${options.taskId}/steps`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          thought: "подтверждение",
          tool_name: `computer.${step.action.type}`,
          tool_args: { step_id: step.stepId, answer },
          confirmation: answer === "reject" ? "rejected" : "approved",
        }),
      });
    },
    async hideSelf() {
      try {
        await (await heliWindow()).minimize();
        await new Promise((done) => setTimeout(done, 250));
      } catch {
        // window control is best effort
      }
    },
    async showSelf() {
      try {
        const win = await heliWindow();
        await win.unminimize();
        await win.setFocus();
      } catch {
        // window control is best effort
      }
    },
    stopped: options.isStopped,
  };
}

/** Entry point for the client-action dispatcher: runs one use_computer call. */
export async function runUseComputerOnDesktop(goal: string, options: DesktopComputerOptions): Promise<ComputerOutcome> {
  if (!isDesktop()) {
    return { content: "Управление компьютером доступно только в приложении Heli для Windows.", error: "failed", steps: 0 };
  }
  try {
    return await runUseComputer(goal, desktopComputerPorts(options));
  } finally {
    computerConfirm.cancel();
  }
}
