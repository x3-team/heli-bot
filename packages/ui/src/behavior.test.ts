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

test("the agent computer panel can be narrowed but leaves room for the chat", async () => {
  const { clampComputerWidth } = await import("./behavior.ts");
  assert.equal(clampComputerWidth(100, 248, 1920), 300);
  assert.equal(clampComputerWidth(700, 248, 1920), 700);
  assert.equal(clampComputerWidth(1800, 248, 1920), 1920 - 248 - 360);
  assert.equal(clampComputerWidth(1800, 72, 1000), 1000 - 72 - 360);
  assert.equal(clampComputerWidth(500, 248, 600), 300);
});
