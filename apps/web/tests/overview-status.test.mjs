import assert from "node:assert/strict";
import test from "node:test";

import { overviewDegradedReason, overviewTradeEligible, parseOverviewResponse } from "../app/overview-status.ts";

const degraded = {
  status: "degraded",
  mode: "paper",
  dependencies: { database: "ok", trading: "stale", redis: "ok" },
};

test("expected health 503 is a degraded snapshot and names the failing dependency", async () => {
  const value = await parseOverviewResponse("health", Response.json(degraded, { status: 503 }));
  assert.deepEqual(value, degraded);
  assert.equal(overviewDegradedReason(value, { trade_eligible: true, reasons: [] }, { quality_status: "ok" }), "trading: stale");
});

test("BFF outage and other 503 responses remain errors", async () => {
  await assert.rejects(parseOverviewResponse("health", Response.json({ code: "BACKEND_UNAVAILABLE" }, { status: 503 })), /BACKEND_UNAVAILABLE/);
  await assert.rejects(parseOverviewResponse("markets", Response.json(degraded, { status: 503 })), /HTTP 503/);
});

test("market quality reason takes precedence when market is stale", () => {
  assert.equal(overviewDegradedReason(degraded, { trade_eligible: false, reasons: ["ticker_stale"] }, { quality_status: "stale" }), "ticker_stale");
});

test("health degradation or a failed refresh never enables Paper start", () => {
  const quality = { trade_eligible: true };
  const ticker = { quality_status: "ok" };
  assert.equal(overviewTradeEligible("ready", degraded, quality, ticker), false);
  assert.equal(overviewTradeEligible("error", { status: "ok" }, quality, ticker), false);
  assert.equal(overviewTradeEligible("ready", { status: "ok" }, quality, ticker), true);
});
