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

export async function checkForAppUpdates(): Promise<void> {
  if (!tauri()) return;
  try {
    const { check } = await import("@tauri-apps/plugin-updater");
    const update = await check();
    if (update) await update.downloadAndInstall();
  } catch {
    // updater optional until release signing is configured
  }
}
