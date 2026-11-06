import assert from "node:assert/strict";
import { test } from "node:test";
import { MAIL_CHOICES, REASONS, feedbackRequest } from "./feedback.ts";

test("thumbs keep the old meaning: up is success, down is fail", () => {
  assert.deepEqual(feedbackRequest({ kind: "up" }), { body: { rating: 1, acceptance: "accepted" }, outcome: "success" });
  assert.deepEqual(feedbackRequest({ kind: "down" }), { body: { rating: -1, acceptance: "rejected" }, outcome: "fail" });
});

test("a reason after thumbs down is one tap and does not move the outcome again", () => {
  const { body, outcome } = feedbackRequest({ kind: "reason", code: "incomplete" });
  assert.equal(body.reason_code, "incomplete");
  assert.equal(body.rating, -1);
  assert.equal(outcome, null);
  assert.equal(REASONS.length, 7);
  assert.equal(REASONS[0].label, "Неверные данные");
});

test("«Исправил сам» sends the fix as an edit", () => {
  const { body, outcome } = feedbackRequest({ kind: "fixed", text: " Правильно так " });
  assert.deepEqual(body, { rating: -1, acceptance: "edited", user_fixed: true, user_edit: "Правильно так" });
  assert.equal(outcome, "fail");
});

test("a letter has three answers", () => {
  assert.deepEqual(MAIL_CHOICES.map((c) => c.code), ["sent_as_is", "edited_then_sent", "declined"]);
  assert.equal(feedbackRequest({ kind: "mail", choice: "sent_as_is" }).outcome, "success");
  const edited = feedbackRequest({ kind: "mail", choice: "edited_then_sent", text: "Итоговый текст" });
  assert.equal(edited.body.user_edit, "Итоговый текст");
  assert.equal(edited.body.acceptance, "edited");
  const declined = feedbackRequest({ kind: "mail", choice: "declined" });
  assert.deepEqual(declined.body, { mail_disposition: "declined" });
  assert.equal(declined.outcome, null);
});
