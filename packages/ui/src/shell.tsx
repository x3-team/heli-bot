import { useState } from "react";
import type { AppView, MascotState } from "./types";

const VIEWS: { id: AppView; label: string }[] = [
  { id: "chat", label: "Задачи" },
  { id: "usage", label: "Использование" },
  { id: "schedules", label: "Расписания" },
  { id: "credentials", label: "Доступы" },
  { id: "memory", label: "Память" },
  { id: "settings", label: "Настройки" },
];

export function MascotSlot({ state }: { state: MascotState }) {
  return (
    <div
      className="mascot-slot"
      data-design-slot="mascot"
      data-mascot-state={state}
      aria-hidden="true"
    />
  );
}

export function Shell() {
  const [view, setView] = useState<AppView>("chat");
  const [mascot] = useState<MascotState>("idle");

  return (
    <div className="app" data-design-slot="app-shell" data-view={view}>
      <aside className="sidebar" data-design-slot="sidebar">
        <MascotSlot state={mascot} />
        <button type="button" data-emphasis="primary" data-design-slot="new-task">
          Новая задача
        </button>
        <div data-design-slot="chat-list">
          <p className="slot-label">Список агентов и чатов</p>
        </div>
        <nav className="nav" data-design-slot="nav">
          {VIEWS.map((item) => (
            <button
              key={item.id}
              type="button"
              aria-current={view === item.id ? "page" : undefined}
              onClick={() => setView(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>
      </aside>
      <section className="main" data-design-slot="main">
        {view === "chat" ? <ChatPlaceholder /> : null}
        {view === "usage" ? <UsagePlaceholder /> : null}
        {view === "schedules" ? <SchedulesPlaceholder /> : null}
        {view === "credentials" ? <CredentialsPlaceholder /> : null}
        {view === "memory" ? <MemoryPlaceholder /> : null}
        {view === "settings" ? <SettingsPlaceholder /> : null}
      </section>
    </div>
  );
}

function ChatPlaceholder() {
  return (
    <div className="thread" data-design-slot="chat">
      <p className="slot-label">Ход выполнения: шаги и скриншоты</p>
      <div className="card" data-design-slot="confirmation-card">
        <p className="slot-label">Карточка подтверждения</p>
        <div className="composer">
          <button type="button" data-emphasis="primary">Разрешить</button>
          <button type="button">Изменить</button>
          <button type="button" data-emphasis="danger">Отклонить</button>
          <button type="button">Стоп</button>
        </div>
      </div>
      <form className="composer" data-design-slot="composer" onSubmit={(e) => e.preventDefault()}>
        <textarea aria-label="Текст задачи" placeholder="Опишите задачу" />
        <button type="submit" data-emphasis="primary">Отправить</button>
      </form>
    </div>
  );
}

function UsagePlaceholder() {
  return (
    <div className="card" data-design-slot="usage">
      <h1>Использование</h1>
      <p className="slot-label">Использовано —% недели · —% месяца, сброс пн 00:00 МСК</p>
    </div>
  );
}

function SchedulesPlaceholder() {
  return (
    <div className="card" data-design-slot="schedules">
      <h1>Расписания</h1>
      <p className="slot-label">Список расписаний</p>
    </div>
  );
}

function CredentialsPlaceholder() {
  return (
    <div className="card" data-design-slot="credentials">
      <h1>Доступы</h1>
      <p className="slot-label">Сайты и учётные записи. Секреты не показываются.</p>
    </div>
  );
}

function MemoryPlaceholder() {
  return (
    <div className="card" data-design-slot="memory">
      <h1>Память</h1>
      <p className="slot-label">Факты, которые можно удалить</p>
    </div>
  );
}

function SettingsPlaceholder() {
  return (
    <div className="card" data-design-slot="settings">
      <h1>Настройки</h1>
      <p className="slot-label">Разрешения компьютера и согласия</p>
      <label>
        <input type="checkbox" disabled /> Согласие на обработку персональных данных
      </label>
      <label>
        <input type="checkbox" disabled /> Использовать мои задачи для улучшения модели
      </label>
    </div>
  );
}
