import assert from "node:assert/strict";
import test from "node:test";

import { parseAuthResponse } from "./api";

test("parseAuthResponse maps trial flag from register/login payload", () => {
  const trial = parseAuthResponse({ access_token: "tok", trial_active: true });
  assert.equal(trial.accessToken, "tok");
  assert.equal(trial.trialActive, true);

  const noTrial = parseAuthResponse({ access_token: "tok2" });
  assert.equal(noTrial.trialActive, false);
});
