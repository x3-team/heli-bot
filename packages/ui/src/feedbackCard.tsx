import { useState } from "react";
import { MAIL_CHOICES, REASONS, type FeedbackAction } from "./feedback";

type Screen = "main" | "down" | "fix" | "comment" | "mail" | "thanks";

/**
 * Rating card after every finished task (stopped ones too). It never blocks the input: a new message
 * simply removes it, ✕ closes it without a rating.
 */
export function FeedbackCard({
  mail,
  onAction,
  onClose,
}: {
  mail: boolean;
  onAction: (action: FeedbackAction) => Promise<boolean>;
  onClose: (rated: boolean) => void;
}) {
  const [screen, setScreen] = useState<Screen>("main");
  const [text, setText] = useState("");
  const [reason, setReason] = useState<string | null>(null);
  const [rated, setRated] = useState(false);
  const [busy, setBusy] = useState(false);

  async function act(action: FeedbackAction, next: Screen | "close") {
    if (busy) return;
    setBusy(true);
    const ok = await onAction(action).catch(() => false);
    setBusy(false);
    if (!ok) return;
    setRated(true);
    setText("");
    if (next === "close") onClose(true);
    else setScreen(next);
  }

  const close = (
    <button type="button" className="feedback-close" aria-label="Закрыть" title="Закрыть" onClick={() => onClose(rated)}>
      ×
    </button>
  );

  if (screen === "thanks") {
    return (
      <article className="card feedback-card">
        {close}
        <p className="muted">Спасибо, отзыв сохранён.</p>
      </article>
    );
  }

  if (screen === "fix" || screen === "mail" || screen === "comment") {
    const label = screen === "fix" ? "Как правильно?" : screen === "mail" ? "Какой текст ушёл в итоге?" : "Комментарий";
    const submit = () => {
      const value = text.trim();
      if (screen === "fix") {
        if (value) void act({ kind: "fixed", text: value }, "thanks");
      } else if (screen === "mail") {
        void act({ kind: "mail", choice: "edited_then_sent", text: value }, "thanks");
      } else if (value) {
        void act({ kind: "comment", text: value }, "thanks");
      }
    };
    return (
      <article className="card feedback-card">
        {close}
        <p className="muted">{label}</p>
        <textarea aria-label={label} placeholder={label} value={text} onChange={(event) => setText(event.target.value)} />
        <div className="row">
          <button type="button" data-emphasis="primary" disabled={busy} onClick={submit}>
            Отправить
          </button>
          <button type="button" onClick={() => setScreen(rated ? "down" : "main")}>
            Назад
          </button>
        </div>
      </article>
    );
  }

  if (screen === "down") {
    return (
      <article className="card feedback-card">
        {close}
        <p className="muted">Что пошло не так?</p>
        <div className="row feedback-reasons">
          {REASONS.map((item) => (
            <button
              key={item.code}
              type="button"
              aria-pressed={reason === item.code}
              disabled={busy}
              onClick={() => {
                setReason(item.code);
                void act({ kind: "reason", code: item.code }, "down");
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="row">
          <button type="button" onClick={() => setScreen("fix")}>Исправил сам</button>
          <button type="button" onClick={() => setScreen("comment")}>Добавить комментарий</button>
        </div>
      </article>
    );
  }

  return (
    <article className="card feedback-card">
      {close}
      <p className="muted">Как прошла задача?</p>
      {mail ? (
        <>
          <p className="muted">Что с письмом?</p>
          <div className="row">
            {MAIL_CHOICES.map((item) => (
              <button
                key={item.code}
                type="button"
                disabled={busy}
                onClick={() => {
                  if (item.code === "edited_then_sent") setScreen("mail");
                  else void act({ kind: "mail", choice: item.code }, "thanks");
                }}
              >
                {item.label}
              </button>
            ))}
          </div>
        </>
      ) : null}
      <div className="row">
        <button type="button" data-emphasis="primary" disabled={busy} onClick={() => void act({ kind: "up" }, "thanks")}>
          👍 Сработало
        </button>
        <button type="button" disabled={busy} onClick={() => void act({ kind: "down" }, "down")}>
          👎 Не сработало
        </button>
        <button type="button" disabled={busy} onClick={() => setScreen("fix")}>
          Исправил сам
        </button>
      </div>
      <button type="button" className="link" onClick={() => setScreen("comment")}>
        Добавить комментарий
      </button>
    </article>
  );
}
