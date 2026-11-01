import { computerConfirm } from "./computerDesktop";

const LOG_LIMIT = 200;
const logLines: string[] = [];

export function pushClientLog(line: string): void {
  logLines.push(line);
  if (logLines.length > LOG_LIMIT) logLines.splice(0, logLines.length - LOG_LIMIT);
}

export function readClientLog(): string {
  return logLines.join("\n");
}

function tauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export async function handControlBack(): Promise<void> {
  computerConfirm.cancel();
  if (!tauri()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("agent_set_stopped", { stopped: true });
}

export async function resetAgentControl(): Promise<void> {
  if (!tauri()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("agent_set_stopped", { stopped: false });
}

export async function captureScreenshotBase64(): Promise<string | null> {
  if (!tauri()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return (await invoke<string>("computer_screenshot")) ?? null;
  } catch {
    return null;
  }
}

export async function tryLaunchLocalBrowser(): Promise<{ browser: "chrome" | "edge"; port: number } | null> {
  if (!tauri()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return await invoke<{ browser: "chrome" | "edge"; port: number }>("browser_launch");
  } catch {
    return null;
  }
}

export type ReadyUpdate = {
  version: string;
  install: () => Promise<void>;
};

export type UpdateCheck =
  | { kind: "web" }
  | { kind: "latest" }
  | { kind: "ready"; update: ReadyUpdate }
  | { kind: "error"; message: string };

export async function appVersion(): Promise<string | null> {
  if (!tauri()) return null;
  try {
    const { getVersion } = await import("@tauri-apps/api/app");
    return await getVersion();
  } catch {
    return null;
  }
}

/**
 * Looks for a newer signed build and downloads it in the background.
 * Installing on Windows closes the app, runs the installer quietly and opens Heli again.
 */
export async function prepareAppUpdate(): Promise<UpdateCheck> {
  if (!tauri()) return { kind: "web" };
  try {
    const { check } = await import("@tauri-apps/plugin-updater");
    const update = await check();
    if (!update) return { kind: "latest" };
    await update.download();
    pushClientLog(`update ${update.currentVersion} -> ${update.version} downloaded`);
    return { kind: "ready", update: { version: update.version, install: () => update.install() } };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    pushClientLog(`update check failed: ${message}`);
    return { kind: "error", message };
  }
}

/** Opens a link from a reply in the system browser (desktop) or a new tab (web). */
export async function openLink(href: string): Promise<void> {
  if (!tauri()) {
    window.open(href, "_blank", "noopener,noreferrer");
    return;
  }
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("link_open", { url: href });
}
