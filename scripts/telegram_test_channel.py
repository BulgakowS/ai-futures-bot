"""One-shot Telegram test-channel preview. No database or alert outbox is opened.

Default: .venv/bin/python scripts/telegram_test_channel.py
External send requires a separately authorized `--send` run and an explicit test chat ID.
"""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
from dotenv import dotenv_values

TEST_TEXT = "AI Futures paper TEST: one-shot alert delivery check. No trading incident."
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


async def send_one_shot(client: httpx.AsyncClient, token: str, chat_id: str) -> bool:
    response = await client.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": TEST_TEXT},
    )
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return body.get("ok") is True and isinstance(body.get("result"), dict)


async def preview():
    requests = 0

    def mock(request):
        nonlocal requests
        requests += 1
        assert request.url.host == "api.telegram.org"
        assert json.loads(request.content) == {"chat_id": "local-test-channel", "text": TEST_TEXT}
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as client:
        accepted = await send_one_shot(client, "local-preview-token", "local-test-channel")
    assert accepted and requests == 1
    print(
        json.dumps(
            {
                "mode": "preview",
                "mock_requests": 1,
                "outbox_used": False,
                "external_messages": 0,
                "text": TEST_TEXT,
            }
        )
    )


def channel_config(env_file: Path):
    saved = dotenv_values(env_file, interpolate=False) if env_file.is_file() else {}

    def value(name):
        return os.environ.get(name, saved.get(name) or "")

    token = value("TELEGRAM_BOT_TOKEN")
    test_chat = value("TELEGRAM_TEST_CHAT_ID")
    production_chat = value("TELEGRAM_CHAT_ID")
    if not token or not test_chat or (production_chat and test_chat == production_chat):
        raise SystemExit("A token and separate test chat are required")
    return token, test_chat


async def send(env_file: Path):
    token, test_chat = channel_config(env_file)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            accepted = await send_one_shot(client, token, test_chat)
    except Exception:
        raise SystemExit("Test-channel delivery failed") from None
    print(
        json.dumps(
            {"mode": "send", "accepted": accepted, "outbox_used": False, "external_requests": 1}
        )
    )
    if not accepted:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-config", action="store_true", help="validate channel variables without sending"
    )
    parser.add_argument(
        "--send", action="store_true", help="send once to a separately configured test chat"
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    args = parser.parse_args()
    if args.check_config:
        channel_config(args.env_file)
        print(json.dumps({"mode": "config-check", "ready": True, "external_messages": 0}))
    else:
        asyncio.run(send(args.env_file) if args.send else preview())
