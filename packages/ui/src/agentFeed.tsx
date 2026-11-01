import type { AgentEvent } from "./sse";

export interface FeedItem {
  key: string;
  icon: string;
  text: string;
  tone?: "error" | "muted";
}

const TOOL_LABELS: Record<string, string> = {
  web_search: "Ищу",
  open_url: "Читаю",
  browser_open: "Открываю",
  browser_read: "Смотрю страницу",
  browser_click: "Нажимаю",
  browser_type: "Ввожу текст",
  browser_press: "Клавиша",
  browser_screenshot: "Снимок экрана",
  ask_confirmation: "Спрашиваю разрешение",
  use_computer: "Работаю на компьютере",
  save_file: "Кладу файл вам на компьютер",
  read_file: "Читаю ваш файл",
  browser_upload: "Загружаю ваш файл на сайт",
};

function argSummary(tool: string, args: Record<string, unknown>): string {
  if (tool === "web_search") return `«${String(args.query ?? "")}»`;
  if (tool === "open_url" || tool === "browser_open") return String(args.url ?? "");
  if (tool === "browser_click") return args.text ? `«${String(args.text)}»` : args.ref != null ? `элемент ${String(args.ref)}` : "";
  if (tool === "browser_type") return args.ref != null ? `в поле ${String(args.ref)}` : "";
  if (tool === "browser_press") return String(args.key ?? "");
  if (tool === "ask_confirmation") return String(args.action ?? "");
  if (tool === "use_computer") return String(args.goal ?? "");
  if (tool === "save_file") return String(args.name ?? "");
  return "";
}

/** Turns stream events into short lines for the «Компьютер агента» panel. Returns null for events not shown. */
export function feedItem(event: AgentEvent): FeedItem | null {
  const key = String(event.seq);
  if (event.type === "step") {
    const kind = event.kind;
    if (kind === "classify") return { key, icon: "◦", text: "Разбираю задачу", tone: "muted" };
    if (kind === "tool_call") {
      const tool = String(event.tool ?? "");
      const label = TOOL_LABELS[tool] ?? tool;
      const detail = argSummary(tool, (event.args as Record<string, unknown>) ?? {});
      return { key, icon: "→", text: detail ? `${label}: ${detail}` : label };
    }
    if (kind === "tool_result" && event.error) {
      return { key, icon: "!", text: `Не вышло: ${String(event.error)}`, tone: "error" };
    }
    if (kind === "model" && typeof event.text === "string" && event.text.trim()) {
      return { key, icon: "◦", text: event.text.trim().slice(0, 160), tone: "muted" };
    }
    if (kind === "answer") return { key, icon: "✓", text: "Готовлю ответ" };
    return null;
  }
  if (event.type === "paused") {
    return {
      key,
      icon: "Ⅱ",
      text: event.reason === "cost" ? "Пауза: дороже оценки" : "Пауза",
      tone: "muted",
    };
  }
  if (event.type === "file") return { key, icon: "↓", text: `Файл в Загрузки › Heli: ${String(event.name ?? "")}` };
  if (event.type === "resumed") return { key, icon: "▶", text: "Продолжаю", tone: "muted" };
  if (event.type === "error") return { key, icon: "!", text: "Ошибка", tone: "error" };
  return null;
}

export function StepFeed({ items }: { items: FeedItem[] }) {
  if (items.length === 0) return null;
  return (
    <ol className="step-feed" aria-label="Шаги агента">
      {items.slice(-12).map((item) => (
        <li key={item.key} data-tone={item.tone}>
          <span aria-hidden="true">{item.icon}</span> {item.text}
        </li>
      ))}
    </ol>
  );
}
