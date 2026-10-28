import assert from "node:assert/strict";
import test from "node:test";

import { permittedHost } from "../app/api/backend/host.ts";

test("production host stays on port 3030 despite a test override", () => {
  assert.equal(permittedHost("localhost:3030", "local", "3100"), true);
  assert.equal(permittedHost("localhost:3100", "local", "3100"), false);
  assert.equal(permittedHost("127.0.0.1:3100", undefined, "3100"), false);
  assert.equal(permittedHost("127.0.0.1:3030", undefined, "3100"), true);
  assert.equal(permittedHost("localhost:3000", "local", "3100"), false);
});

test("test override accepts only exact loopback hosts", () => {
  assert.equal(permittedHost("localhost:3100", "test", "3100"), true);
  assert.equal(permittedHost("127.0.0.1:3100", "test", "3100"), true);
  assert.equal(permittedHost("example.com:3100", "test", "3100"), false);
  assert.equal(permittedHost("localhost.evil:3100", "test", "3100"), false);
  assert.equal(permittedHost("localhost:3101", "test", "3100"), false);
  assert.equal(permittedHost("localhost:3030", "test", "invalid"), true);
});
