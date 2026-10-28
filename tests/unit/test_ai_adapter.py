import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from ai_futures.ai import AiFailure, MarketContext, OpenAIAdapter

from tests.integration.test_ai import policy


def context():
    return MarketContext(
        context_id="a" * 64, timeframe="1m", ticker_event_id=uuid4(),
        ticker_source_time=datetime.now(UTC).isoformat(), bid="99", ask="100",
        candles=[{
            "event_id": uuid4(), "source_time": datetime.now(UTC).isoformat(),
            "open": "100", "high": "101", "low": "99", "close": "100", "volume": "10",
        }], position=None, risk_budget="0.005", config_version="test-v1",
    )


class StubClient:
    def __init__(self, response, calls):
        self.response, self.calls = response, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, headers, json):
        self.calls.append((url, headers, json))
        return self.response


async def test_openai_adapter_uses_strict_schema_and_store_false(monkeypatch):
    calls = []
    body = {"status": "completed", "output": [{
        "type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "action": "WAIT", "confidence": "0.2", "horizon": "1m",
            "reason_codes": ["NO_EDGE"], "explanation": "No edge",
            "invalidation": "Next candle", "entry": None, "stop_loss": None,
            "take_profit": None, "context_id": "a" * 64,
        })}],
    }], "usage": {"input_tokens": 8, "output_tokens": 9}}
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **_: StubClient(httpx.Response(200, json=body), calls)
    )
    result = await OpenAIAdapter("fake-secret").propose(context(), policy())
    assert result["usage"]["input_tokens"] == 8
    url, headers, payload = calls[0]
    assert url == "https://api.openai.com/v1/responses"
    assert headers["Authorization"] == "Bearer fake-secret"
    assert payload["store"] is False
    assert payload["text"]["format"]["strict"] is True
    schema = payload["text"]["format"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])
    assert "fake-secret" not in json.dumps(payload)
    assert "MEXC" not in json.dumps(payload)


async def test_openai_adapter_rate_limit_fails_to_wait(monkeypatch):
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **_: StubClient(httpx.Response(429), [])
    )
    with pytest.raises(AiFailure) as exc:
        await OpenAIAdapter("fake-secret").propose(context(), policy())
    assert exc.value.code == "RATE_LIMIT"
