import assert from "node:assert/strict";
import test from "node:test";
import { clampPercent, confirmationNeeded, limitTone } from "./behavior.ts";

test("limit bar warns at 80 and stops at 100", () => {
  assert.equal(limitTone(79), "ok");
  assert.equal(limitTone(80), "warn");
  assert.equal(limitTone(99), "warn");
  assert.equal(limitTone(100), "stop");
  assert.equal(clampPercent(140), 100);
});

test("dangerous tasks ask before they run", () => {
  assert.equal(confirmationNeeded("Собери статус по задачам"), false);
  assert.equal(confirmationNeeded("Отправь письмо Ивану"), true);
  assert.equal(confirmationNeeded("Удали файл отчёта"), true);
});
