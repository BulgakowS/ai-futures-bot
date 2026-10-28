(() => {
  const portfolioId = "fixture-portfolio-00000000";
  const answers = {
    health: { status: "ok" },
    markets: [{ symbol: "BTC_USDT", rules_version: "fixture-v1", quality: {
      status: "stale", trade_eligible: false,
      reasons: ["disconnect_or_schema_error"], transport: "rest_fallback"
    }}],
    "markets/BTC_USDT/ticker": {
      source: "rest_fallback", source_time: "2026-10-17T07:49:08.896Z",
      quality_status: "ok", data: { fairPrice: "100" }, quality: {
        status: "stale", trade_eligible: false,
        reasons: ["disconnect_or_schema_error"], transport: "rest_fallback"
      }
    },
    "markets/BTC_USDT/candles?timeframe=1m": { candles: [] },
    portfolios: [{ id: portfolioId, name: "Isolated fixture", mode: "paper", epoch: 1,
      equity: "1000", available_balance: "1000", unrealized_pnl: "0" }],
    bots: [{ id: "fixture-bot-00000000", portfolio_id: portfolioId, mode: "paper",
      status: "READY", active_version: 1, updated_at: "2026-10-17T07:49:00Z" }],
    risk: { kill_switch: false, defaults: {} }
  };
  window.__s10mRequests = [];
  window.fetch = async (input, init = {}) => {
    const path = String(input);
    const method = (init.method || "GET").toUpperCase();
    window.__s10mRequests.push({ method, path });
    if (method !== "GET") throw new Error("S10M mock blocked mutation");
    if (!path.startsWith("/api/backend/")) throw new Error("S10M mock blocked non-API fetch");
    const key = path.slice("/api/backend/".length);
    const value = Object.hasOwn(answers, key) ? answers[key] : [];
    return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  return "S10M GET-only mock installed";
})()
