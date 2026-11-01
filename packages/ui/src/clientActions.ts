/** Runs the agent's client-side tools on this computer: browser_* via local Chrome/Edge, confirmations via the UI. */

import type { ClientActionResult } from "./agentRun";
import { BrowserTab } from "./cdp";

export const BROWSER_TOOLS = [
  "browser_open",
  "browser_read",
  "browser_click",
  "browser_type",
  "browser_press",
  "browser_screenshot",
];

export interface ClientRunInfo {
  base: string;
  token: string;
  taskId: string;
  callId: string;
}

export type ExtraHandler = (
  args: Record<string, unknown>,
  info: ClientRunInfo,
  isStopped: () => boolean,
) => Promise<ClientActionResult>;

/** Other modules (PC actions) register their client tools here. */
const extraHandlers = new Map<string, ExtraHandler>();

export function registerClientTool(name: string, handler: ExtraHandler): void {
  extraHandlers.set(name, handler);
}

function tauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** Tools this app can run: the server offers only these to the model. */
export function availableClientTools(): string[] {
  const tools = ["ask_confirmation", ...extraHandlers.keys()];
  // browser_search is not shown to the model: the server sends web_search here when it has no search API key.
  if (tauri()) tools.push(...BROWSER_TOOLS, "browser_search");
  return tools;
}

let tab: BrowserTab | null = null;

async function browserTab(): Promise<BrowserTab> {
  if (tab?.isOpen) return tab;
  const { invoke } = await import("@tauri-apps/api/core");
  const launch = await invoke<{ browser: string; port: number }>("browser_launch");
  const wsUrl = await invoke<string>("browser_page_ws", { port: launch.port });
  tab = await BrowserTab.attach(wsUrl);
  return tab;
}

export interface ClientActionContext {
  /** Show the confirmation card; resolves true when the user allows the action. */
  confirm: (action: string, details: string) => Promise<boolean>;
  /** Short note for the user, e.g. «пройдите капчу в браузере». */
  notice: (text: string) => void;
  /** True while the user pressed Stop or took control back. */
  halted: () => boolean;
  run: ClientRunInfo;
}

export async function runClientAction(
  tool: string,
  args: Record<string, unknown>,
  ctx: ClientActionContext,
): Promise<ClientActionResult> {
  if (ctx.halted()) return { content: "", error: "stopped_by_user" };
  if (tool === "ask_confirmation") {
    const ok = await ctx.confirm(String(args.action ?? ""), String(args.details ?? ""));
    return { content: ok ? "approved" : "rejected" };
  }
  const extra = extraHandlers.get(tool);
  if (extra) return extra(args, ctx.run, ctx.halted);
  if (!BROWSER_TOOLS.includes(tool) && tool !== "browser_search") return { content: "", error: "unknown_tool" };
  try {
    const page = await browserTab();
    switch (tool) {
      case "browser_search": {
        const content = await page.search(String(args.query ?? ""), () =>
          ctx.notice("Яндекс просит пройти капчу. Пройдите её в окне браузера, и я продолжу."),
        );
        ctx.notice("");
        return { content };
      }
      case "browser_open":
        return { content: await page.open(String(args.url ?? "")) };
      case "browser_read":
        return { content: await page.read() };
      case "browser_click":
        return {
          content: await page.click({
            ref: typeof args.ref === "number" ? args.ref : undefined,
            text: typeof args.text === "string" ? args.text : undefined,
          }),
        };
      case "browser_type":
        return {
          content: await page.type({
            ref: typeof args.ref === "number" ? args.ref : undefined,
            text: String(args.text ?? ""),
            submit: args.submit === true,
          }),
        };
      case "browser_press":
        return { content: await page.press(String(args.key ?? "Enter")) };
      case "browser_screenshot": {
        const data = await page.screenshot();
        return { content: "Снимок сделан.", screenshot_base64: data };
      }
    }
  } catch (err) {
    const code = err instanceof Error ? err.message : String(err);
    if (code === "browser_disconnected") tab = null;
    return { content: `Ошибка браузера: ${code}`, error: code.slice(0, 180) };
  }
  return { content: "", error: "unknown_tool" };
}
