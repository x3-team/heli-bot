import assert from "node:assert/strict";
import test from "node:test";

import { parseAuthResponse, trialLabel } from "./authSession.ts";

test("parseAuthResponse maps trial flag from register/login payload", () => {
  const trial = parseAuthResponse({ access_token: "tok", trial_active: true });
  assert.equal(trial.accessToken, "tok");
  assert.equal(trial.trialActive, true);

  const noTrial = parseAuthResponse({ access_token: "tok2" });
  assert.equal(noTrial.trialActive, false);
});

test("trialLabel shows the end date in Moscow time", () => {
  assert.equal(trialLabel("2026-11-07T22:30:00Z"), "Пробная неделя до 8 ноября");
  assert.equal(trialLabel(null), "Пробная неделя");
  assert.equal(trialLabel("not-a-date"), "Пробная неделя");
});
