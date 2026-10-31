import assert from "node:assert/strict";
import test from "node:test";

import { mapErrorCode, readApiError } from "./errors.ts";

test("maps known auth errors", () => {
  assert.match(mapErrorCode("email_taken"), /зарегистрирован/);
  assert.match(mapErrorCode("auth_not_configured"), /JWT/);
});

test("readApiError returns server detail string", async () => {
  const response = new Response(JSON.stringify({ detail: "email_taken" }), {
    status: 409,
    headers: { "content-type": "application/json" },
  });
  assert.match(await readApiError(response), /зарегистрирован/);
});
