"""Loopback-only N4b browser fixture; never reads the real backend or market."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WAIT_ID = "00000000-0000-4000-8000-000000000001"
TEST_ID = "00000000-0000-4000-8000-000000000002"
BASE = {
    "symbol": "BTC_USDT",
    "horizon_hours": 4,
    "created_at": "2026-01-01T16:01:00+00:00",
    "model_version": "synthetic-model-v1",
    "dataset_checksum": "7" * 64,
    "config_checksum": "5" * 64,
    "source_versions": ["synthetic-fixture-v1"],
    "market_event_ids": ["synthetic-market-1", "synthetic-market-2"],
    "news_version_ids": ["synthetic-news-1"],
    "quality": "ok",
}
WAIT = {
    **BASE,
    "forecast_id": WAIT_ID,
    "correlation_id": "00000000-0000-4000-8000-000000000011",
    "anchor": "2026-01-01T12:00:00+00:00",
    "status": "wait",
    "direction": "WAIT",
    "reason": "news_coverage_missing",
    "quality": "wait",
    "model_version": None,
    "dataset_checksum": None,
    "market_event_ids": [],
    "news_version_ids": [],
    "probabilities": None,
    "proposal": None,
    "test_fixture": False,
}
TEST = {
    **BASE,
    "forecast_id": TEST_ID,
    "correlation_id": "00000000-0000-4000-8000-000000000012",
    "anchor": "2026-01-01T16:00:00+00:00",
    "status": "candidate",
    "direction": "UP",
    "reason": "approved_synthetic_candidate",
    "probabilities": {"DOWN": "0.01", "NEUTRAL": "0.01", "UP": "0.98"},
    "proposal": {
        "proposal_id": "00000000-0000-4000-8000-000000000022",
        "direction": "LONG",
        "entry": "101.1",
        "stop_loss": "100.0",
        "take_profit": "103.2",
    },
    "test_fixture": True,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + self.server.backend_token:
            self.send_response(401)
            self.end_headers()
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/v1/research/forecasts":
            payload = {"mode": "collect-only", "trading_enabled": False, "items": [TEST, WAIT]}
        elif path == "/api/v1/research/forecasts/latest":
            payload = {"mode": "collect-only", "trading_enabled": False, "forecast": TEST}
        elif path == f"/api/v1/research/forecasts/{WAIT_ID}":
            payload = {"mode": "collect-only", "trading_enabled": False, "forecast": WAIT}
        elif path == f"/api/v1/research/forecasts/{TEST_ID}":
            payload = {"mode": "collect-only", "trading_enabled": False, "forecast": TEST}
        elif path == "/api/v1/health":
            payload = {"status": "ok"}
        elif path == "/api/v1/markets":
            payload = []
        elif path == "/api/v1/markets/BTC_USDT/ticker":
            payload = {"quality_status": "stale", "quality": {"trade_eligible": False}}
        elif path == "/api/v1/markets/BTC_USDT/candles":
            payload = {"candles": []}
        elif path in {"/api/v1/portfolios", "/api/v1/bots"}:
            payload = []
        elif path == "/api/v1/risk":
            payload = {"kill_switch": True, "defaults": {}}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.send_response(405)
        self.end_headers()

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.backend_token = args.token
    try:
        server.serve_forever()
    finally:
        server.server_close()
