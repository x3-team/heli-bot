/** Rating card after a task: what each tap sends. Pure, so it is tested without React. */

export type ReasonCode =
  | "wrong_fact"
  | "incomplete"
  | "wrong_system"
  | "refused_needed"
  | "dangerous"
  | "slow"
  | "other";

export type MailDisposition = "sent_as_is" | "edited_then_sent" | "declined";

export const REASONS: { code: ReasonCode; label: string }[] = [
  { code: "wrong_fact", label: "Неверные данные" },
  { code: "incomplete", label: "Сделал не всё" },
  { code: "wrong_system", label: "Не туда зашёл" },
  { code: "refused_needed", label: "Не сделал нужное действие" },
  { code: "dangerous", label: "Сделал опасное / не то отправил" },
  { code: "slow", label: "Долго / завис" },
  { code: "other", label: "Другое" },
];

export const MAIL_CHOICES: { code: MailDisposition; label: string }[] = [
  { code: "sent_as_is", label: "Отправил как есть" },
  { code: "edited_then_sent", label: "Правил и отправил" },
  { code: "declined", label: "Не стал отправлять" },
];

export interface FeedbackPayload {
  rating?: 1 | -1;
  acceptance?: "accepted" | "edited" | "rejected";
  comment?: string;
  user_edit?: string;
  reason_code?: ReasonCode;
  user_fixed?: boolean;
  mail_disposition?: MailDisposition;
}

export type FeedbackAction =
  | { kind: "up" }
  | { kind: "down" }
  | { kind: "reason"; code: ReasonCode }
  | { kind: "comment"; text: string }
  | { kind: "fixed"; text: string }
  | { kind: "mail"; choice: MailDisposition; text?: string };

/** The body for POST /v1/tasks/{id}/feedback and the outcome for /finish (null: leave the run's outcome). */
export function feedbackRequest(action: FeedbackAction): {
  body: FeedbackPayload;
  outcome: "success" | "fail" | null;
} {
  switch (action.kind) {
    case "up":
      return { body: { rating: 1, acceptance: "accepted" }, outcome: "success" };
    case "down":
      return { body: { rating: -1, acceptance: "rejected" }, outcome: "fail" };
    case "reason":
      return { body: { rating: -1, acceptance: "rejected", reason_code: action.code }, outcome: null };
    case "comment":
      return { body: { comment: action.text.trim() }, outcome: null };
    case "fixed":
      return {
        body: { rating: -1, acceptance: "edited", user_fixed: true, user_edit: action.text.trim() },
        outcome: "fail",
      };
    case "mail":
      if (action.choice === "sent_as_is") {
        return { body: { rating: 1, acceptance: "accepted", mail_disposition: "sent_as_is" }, outcome: "success" };
      }
      if (action.choice === "edited_then_sent") {
        const text = (action.text ?? "").trim();
        return {
          body: { acceptance: "edited", mail_disposition: "edited_then_sent", ...(text ? { user_edit: text } : {}) },
          outcome: "success",
        };
      }
      // Not sending is the user's call, not a failure of the task.
      return { body: { mail_disposition: "declined" }, outcome: null };
  }
}
