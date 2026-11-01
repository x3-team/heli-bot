/** Streaming agent run: create the task, read the step stream, answer client actions, pause/stop. */

import { ApiError } from "./api";
import { readApiError } from "./errors";
import { parseSse, type AgentEvent } from "./sse";

export { parseSse, stoppedNote, type AgentEvent } from "./sse";

export interface ClientActionResult {
  content: string;
  error?: string;
  screenshot_base64?: string;
}

export interface AgentRunOptions {
  clientTools: string[];
  autopause: boolean;
  estimateRub?: string | null;
  /** Stored with the task, e.g. { files: [ids of attached files] }. */
  context?: Record<string, unknown>;
  onEvent: (event: AgentEvent) => void;
  onClientAction: (tool: string, args: Record<string, unknown>, callId: string, taskId: string) => Promise<ClientActionResult>;
  onTask?: (taskId: string) => void;
  signal: AbortSignal;
}

export interface AgentRunResult {
  taskId: string;
  reply: string;
  stopped: string | null;
  costRub: string;
}

function auth(token: string): Record<string, string> {
  return { authorization: `Bearer ${token}` };
}

export async function controlRun(
  base: string,
  token: string,
  taskId: string,
  action: "pause" | "resume" | "stop",
): Promise<void> {
  const response = await fetch(`${base}/v1/tasks/${taskId}/control`, {
    method: "POST",
    headers: { "content-type": "application/json", ...auth(token) },
    body: JSON.stringify({ action }),
  });
  if (!response.ok && response.status !== 404) throw new ApiError(await readApiError(response), response.status);
}

async function postToolResult(base: string, token: string, taskId: string, callId: string, result: ClientActionResult) {
  await fetch(`${base}/v1/tasks/${taskId}/tool-results`, {
    method: "POST",
    headers: { "content-type": "application/json", ...auth(token) },
    body: JSON.stringify({ call_id: callId, ...result }),
  });
}

async function readStream(
  response: Response,
  onEvent: (event: AgentEvent) => void,
): Promise<number> {
  if (!response.body) throw new ApiError("Сервер не отдал поток шагов.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let last = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSse(buffer);
    buffer = parsed.rest;
    for (const event of parsed.events) {
      last = Math.max(last, event.seq ?? 0);
      onEvent(event);
      if (event.type === "done" || event.type === "error") return -1;
    }
  }
  return last;
}

export async function runAgentTask(
  base: string,
  token: string,
  text: string,
  options: AgentRunOptions,
): Promise<AgentRunResult> {
  const created = await fetch(`${base}/v1/tasks`, {
    method: "POST",
    headers: { "content-type": "application/json", ...auth(token) },
    body: JSON.stringify({ request: text, context: options.context ?? {} }),
    signal: options.signal,
  });
  if (!created.ok) throw new ApiError(await readApiError(created), created.status);
  const { id: taskId } = (await created.json()) as { id: string };
  options.onTask?.(taskId);

  let result: AgentRunResult | null = null;
  let failure: string | null = null;
  const handled = new Set<string>();
  const onEvent = (event: AgentEvent) => {
    options.onEvent(event);
    if (event.type === "client_action") {
      const callId = String(event.call_id);
      if (handled.has(callId)) return;
      handled.add(callId);
      void options
        .onClientAction(String(event.tool), (event.args as Record<string, unknown>) ?? {}, callId, taskId)
        .catch((err: unknown) => ({ content: "", error: err instanceof Error ? err.message : "client_failed" }))
        .then((res) => postToolResult(base, token, taskId, callId, res))
        .catch(() => undefined);
    } else if (event.type === "done") {
      result = {
        taskId,
        reply: String(event.reply ?? ""),
        stopped: (event.stopped as string | null) ?? null,
        costRub: String(event.cost_rub ?? "0"),
      };
    } else if (event.type === "error") {
      failure = String(event.code ?? "run_failed");
    }
  };
  const stopOnAbort = () => void controlRun(base, token, taskId, "stop").catch(() => undefined);
  options.signal.addEventListener("abort", stopOnAbort, { once: true });

  try {
    const response = await fetch(`${base}/v1/tasks/${taskId}/run`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream", ...auth(token) },
      body: JSON.stringify({
        client_tools: options.clientTools,
        autopause: options.autopause,
        estimate_rub: options.estimateRub ?? null,
      }),
      signal: options.signal,
    });
    if (!response.ok) throw new ApiError(await readApiError(response), response.status);
    let last = await readStream(response, onEvent);
    // The connection may drop on a long run (proxy timeouts): pick the stream up where it stopped.
    for (let retry = 0; last >= 0 && !result && !failure && retry < 20; retry++) {
      if (options.signal.aborted) break;
      await new Promise((r) => setTimeout(r, Math.min(1000 * (retry + 1), 5000)));
      const again = await fetch(`${base}/v1/tasks/${taskId}/events?after=${last}`, {
        headers: { accept: "text/event-stream", ...auth(token) },
        signal: options.signal,
      });
      if (!again.ok) break;
      const next = await readStream(again, onEvent);
      last = next < 0 ? -1 : Math.max(last, next);
    }
  } finally {
    options.signal.removeEventListener("abort", stopOnAbort);
  }
  if (failure) throw new ApiError(runErrorMessage(failure));
  if (!result) throw new ApiError("Связь с сервером прервалась, задача не завершилась.");
  return result;
}

export function runErrorMessage(code: string): string {
  switch (code) {
    case "limit_reached":
      return "Лимит исчерпан. Задача остановлена.";
    case "missing_api_key":
    case "request_failed":
      return "Модель сейчас недоступна. Попробуйте ещё раз через минуту.";
    default:
      return "Не получилось выполнить задачу.";
  }
}

