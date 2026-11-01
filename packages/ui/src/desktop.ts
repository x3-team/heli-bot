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

/**
 * Looks for a newer signed build and downloads it in the background.
 * Returns null when there is nothing to do (web build, no update, offline).
 * Installing on Windows closes the app, runs the installer quietly and opens Heli again.
 */
export async function prepareAppUpdate(): Promise<ReadyUpdate | null> {
  if (!tauri()) return null;
  try {
    const { check } = await import("@tauri-apps/plugin-updater");
    const update = await check();
    if (!update) return null;
    await update.download();
    pushClientLog(`update ${update.currentVersion} -> ${update.version} downloaded`);
    return { version: update.version, install: () => update.install() };
  } catch (err) {
    pushClientLog(`update check failed: ${err instanceof Error ? err.message : String(err)}`);
    return null;
  }
}
