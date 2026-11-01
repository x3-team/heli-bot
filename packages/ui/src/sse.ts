/** Pure helpers for the agent step stream (no imports: tested with plain Node). */

export interface AgentEvent {
  seq: number;
  type: "started" | "step" | "client_action" | "paused" | "resumed" | "notice" | "screen" | "file" | "done" | "error";
  [key: string]: unknown;
}

/** Split an SSE text buffer into complete events; returns the events and the unparsed rest. */
export function parseSse(buffer: string): { events: AgentEvent[]; rest: string } {
  const events: AgentEvent[] = [];
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    const data = block
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data) continue;
    try {
      events.push(JSON.parse(data) as AgentEvent);
    } catch {
      // skip a broken event, the stream goes on
    }
  }
  return { events, rest };
}

export function stoppedNote(stopped: string | null): string | null {
  switch (stopped) {
    case null:
      return null;
    case "stop":
      return "Остановлено. Управление снова у вас.";
    case "pause":
      return "Задача долго стояла на паузе и остановлена.";
    case "confirm":
      return "Действие не подтверждено, задача остановлена.";
    case "budget":
      return "Задача упёрлась в лимит стоимости.";
    case "max_steps":
      return "Задача заняла слишком много шагов и остановлена.";
    case "client_timeout":
      return "Приложение не ответило вовремя, задача остановлена.";
    default:
      return "Задача остановлена.";
  }
}
