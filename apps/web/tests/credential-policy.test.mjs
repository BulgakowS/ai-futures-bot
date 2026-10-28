import assert from "node:assert/strict";
import test from "node:test";

import { allowedPathMethod } from "../app/api/backend/policy.ts";

const id = "123e4567-e89b-12d3-a456-426614174000";

test("credential BFF routes expose only intended methods", () => {
  for (const [path, methods] of [
    ["exchange-credentials", ["GET", "POST"]],
    [`exchange-credentials/${id}`, ["GET"]],
    [`exchange-credentials/${id}/rotate`, ["PUT"]],
    [`exchange-credentials/${id}/revoke`, ["POST"]],
    [`bots/${id}/live-credential`, ["GET", "PUT"]],
  ]) {
    for (const method of ["GET", "POST", "PUT", "DELETE"]) {
      assert.equal(allowedPathMethod(path, method), methods.includes(method), `${method} ${path}`);
    }
  }
  assert.equal(allowedPathMethod(`exchange-credentials/${id}/decrypt`, "GET"), false);
  assert.equal(allowedPathMethod("exchange-credentials/not-a-uuid", "GET"), false);
});

test("existing Paper paths stay available", () => {
  assert.equal(allowedPathMethod("portfolios", "POST"), true);
  assert.equal(allowedPathMethod(`portfolios/${id}/positions/${id}/close`, "POST"), true);
  assert.equal(allowedPathMethod(`bots/${id}/start`, "POST"), true);
  assert.equal(allowedPathMethod(`bots/${id}/live-pilot-settings`, "PUT"), true);
});
