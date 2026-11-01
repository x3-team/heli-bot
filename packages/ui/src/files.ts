/** Files between Heli's cloud computer and this PC: attach in the chat, receive into Загрузки/Heli. */

import { ApiError } from "./api";
import { readApiError } from "./errors";

export const MAX_FILE_BYTES = 25 * 1024 * 1024;

export interface RemoteFile {
  id: string;
  name: string;
  size: number;
  direction: "to_pc" | "from_pc";
  delivered: boolean;
  risky: boolean;
}

export interface SavedFile {
  id: string;
  name: string;
  size: number;
  risky: boolean;
  /** Full path on this PC (desktop app). Absent in the web build, where the browser saved it. */
  path?: string;
  folder?: string;
}

function auth(token: string): Record<string, string> {
  return { authorization: `Bearer ${token}` };
}

function tauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function canOpenFiles(): boolean {
  return tauri();
}

export function formatSize(size: number): string {
  if (size < 1024) return `${size} Б`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} КБ`;
  return `${(size / 1024 / 1024).toFixed(1)} МБ`;
}

export async function uploadAttachment(base: string, token: string, file: File): Promise<RemoteFile> {
  if (file.size > MAX_FILE_BYTES) throw new ApiError(`«${file.name}» больше 25 МБ.`);
  const response = await fetch(`${base}/v1/files`, {
    method: "POST",
    headers: {
      ...auth(token),
      "content-type": "application/octet-stream",
      "x-file-name": encodeURIComponent(file.name),
    },
    body: file,
  });
  if (!response.ok) throw new ApiError(await readApiError(response), response.status);
  return (await response.json()) as RemoteFile;
}

export async function pendingFiles(base: string, token: string): Promise<RemoteFile[]> {
  const response = await fetch(`${base}/v1/files?pending=true`, { headers: auth(token) });
  if (!response.ok) return [];
  const body = (await response.json()) as { files?: RemoteFile[] };
  return body.files ?? [];
}

async function saveOnThisComputer(name: string, bytes: Uint8Array): Promise<{ path?: string; folder?: string; name: string }> {
  if (tauri()) {
    const { invoke } = await import("@tauri-apps/api/core");
    const saved = await invoke<{ path: string; folder: string; name: string }>("file_save", bytes, {
      headers: { "x-file-name": encodeURIComponent(name) },
    });
    try {
      const { getCurrentWindow, UserAttentionType } = await import("@tauri-apps/api/window");
      await getCurrentWindow().requestUserAttention(UserAttentionType.Informational);
    } catch {
      // flashing the taskbar is a nicety
    }
    return saved;
  }
  // Web build: let the browser save it to its downloads.
  const url = URL.createObjectURL(new Blob([bytes as BlobPart], { type: "application/octet-stream" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  return { name };
}

/** Download one file from the server, put it on this PC, tell the server it arrived. */
export async function deliverFile(base: string, token: string, file: RemoteFile): Promise<SavedFile> {
  const response = await fetch(`${base}/v1/files/${file.id}/content`, { headers: auth(token) });
  if (!response.ok) throw new ApiError(await readApiError(response), response.status);
  const bytes = new Uint8Array(await response.arrayBuffer());
  const saved = await saveOnThisComputer(file.name, bytes);
  await fetch(`${base}/v1/files/${file.id}/delivered`, { method: "POST", headers: auth(token) }).catch(() => undefined);
  return { id: file.id, name: saved.name, size: file.size, risky: file.risky, path: saved.path, folder: saved.folder };
}

/** Opens a saved file in its usual program. The app refuses programs and scripts. */
export async function openSavedFile(path: string): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("file_open", { path });
}

/** Shows the file in Explorer / Finder, or the Heli folder when no path is given. */
export async function revealSavedFile(path?: string): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("file_reveal", { path: path ?? null });
}
