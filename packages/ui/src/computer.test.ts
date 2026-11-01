import assert from "node:assert/strict";
import test from "node:test";
import {
  describe,
  markerPosition,
  parseStep,
  runUseComputer,
  type ComputerAction,
  type ComputerPorts,
  type ComputerStep,
  type ConfirmAnswer,
} from "./computer.ts";

function step(action: ComputerAction, risk: ComputerStep["risk"] = "write"): ComputerStep {
  return { stepId: "s", action, thought: "", risk, finished: action.type === "done" || action.type === "fail", error: null };
}

function fake(steps: ComputerStep[], answers: ConfirmAnswer[] = [], opts: { decide?: "allow" | "confirm" | "deny" } = {}) {
  const acted: ComputerAction[] = [];
  const asked: ComputerAction[] = [];
  let stop = false;
  const ports: ComputerPorts = {
    shot: async () => ({ pngBase64: "AAAA", width: 10, height: 10 }),
    step: async () => {
      const next = steps.shift();
      if (!next) throw new Error("no more steps");
      return next;
    },
    decide: async (_action, flagged) => (flagged ? "confirm" : (opts.decide ?? "confirm")),
    confirm: async (request) => {
      asked.push(request.action);
      return answers.shift() ?? "approve";
    },
    act: async (action) => {
      acted.push(action);
    },
    stopped: () => stop,
    sleep: async () => undefined,
  };
  return { ports, acted, asked, stopNow: () => (stop = true) };
}

test("clicks after approval and reports done", async () => {
  const { ports, acted, asked } = fake([
    step({ type: "click", x: 10, y: 20, target: "Документы" }),
    step({ type: "done", summary: "Открыл документы" }),
  ]);
  const outcome = await runUseComputer("Открыть документы", ports);
  assert.equal(outcome.error, undefined);
  assert.match(outcome.content, /Готово: Открыл документы/);
  assert.match(outcome.content, /нажать «Документы»/);
  assert.equal(acted.length, 1);
  assert.equal(asked.length, 1);
});

test("reject stops the goal and does nothing", async () => {
  const { ports, acted } = fake([step({ type: "click", x: 1, y: 1, target: "Провести" }, "danger")], ["reject"]);
  const outcome = await runUseComputer("Провести счёт", ports);
  assert.equal(outcome.error, "rejected");
  assert.equal(acted.length, 0);
});

test("approve-all skips ordinary cards but danger still asks", async () => {
  const { ports, asked, acted } = fake(
    [
      step({ type: "click", x: 1, y: 1, target: "Поле" }),
      step({ type: "type", text: "123" }),
      step({ type: "click", x: 2, y: 2, target: "Оплатить" }, "danger"),
      step({ type: "done" }),
    ],
    ["all", "approve"],
  );
  await runUseComputer("цель", ports);
  assert.deepEqual(
    asked.map((a) => a.target ?? a.type),
    ["Поле", "Оплатить"],
  );
  assert.equal(acted.length, 3);
});

test("stop ends the loop before the next look", async () => {
  const { ports, acted, stopNow } = fake([step({ type: "click", x: 1, y: 1 }), step({ type: "click", x: 2, y: 2 })]);
  ports.act = async (action) => {
    acted.push(action);
    stopNow();
  };
  const outcome = await runUseComputer("цель", ports);
  assert.equal(outcome.error, "stopped");
  assert.equal(acted.length, 1);
});

test("fail asks for a human, step limit is enforced", async () => {
  const failed = await runUseComputer("войти", fake([step({ type: "fail", summary: "Нужен пароль" })]).ports);
  assert.equal(failed.error, "needs_human");
  assert.match(failed.content, /Нужен пароль/);
  const waits = Array.from({ length: 5 }, () => step({ type: "wait", seconds: 1 }, "read"));
  const limited = await runUseComputer("ждать", fake(waits, [], { decide: "allow" }).ports, 3);
  assert.equal(limited.error, "step_limit");
});

test("denied steps go back to the model, three in a row give up", async () => {
  const denied = Array.from({ length: 3 }, () => step({ type: "key", keys: "win+r" }));
  const { ports, acted } = fake(denied, [], { decide: "deny" });
  const outcome = await runUseComputer("цель", ports);
  assert.equal(outcome.error, "failed");
  assert.equal(acted.length, 0);
});

test("wording, marker and server parsing", () => {
  assert.equal(describe({ type: "type", text: "Иванов", target: "поле «Клиент»" }), "ввести «Иванов» в поле «Клиент»");
  assert.equal(describe({ type: "key", keys: "ctrl+s" }), "нажать Ctrl+S");
  assert.deepEqual(markerPosition({ type: "click", x: 500, y: 250 }), { left: "50%", top: "25%" });
  assert.equal(markerPosition({ type: "key", keys: "enter" }), null);
  const parsed = parseStep({ step_id: "1", action: { type: "click", x: 1, y: 2 }, risk: "danger", finished: false, error: null });
  assert.equal(parsed.risk, "danger");
  assert.equal(parsed.action.type, "click");
});
