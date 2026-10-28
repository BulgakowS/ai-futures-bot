"""The one-shot preview must never read the alert backlog or use an external transport."""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/telegram_test_channel.py"
CHANNEL_KEYS = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_TEST_CHAT_ID", "TELEGRAM_CHAT_ID"}


def clean_env():
    return {key: value for key, value in os.environ.items() if key not in CHANNEL_KEYS}


def check_config(env_file, env):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--check-config", "--env-file", str(env_file)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_one_shot_fixed_test_text_uses_one_mock_request():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=True
    )
    preview = json.loads(result.stdout)
    assert preview == {
        "mode": "preview",
        "mock_requests": 1,
        "outbox_used": False,
        "external_messages": 0,
        "text": "AI Futures paper TEST: one-shot alert delivery check. No trading incident.",
    }


def test_send_rejects_same_production_and_test_chat_before_network():
    env = clean_env() | {
        "TELEGRAM_BOT_TOKEN": "local-token",
        "TELEGRAM_TEST_CHAT_ID": "same-chat",
        "TELEGRAM_CHAT_ID": "same-chat",
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check-config"], capture_output=True, text=True, env=env
    )
    assert result.returncode != 0
    assert "separate test chat" in result.stderr


def test_test_chat_without_production_channel_is_valid_offline():
    env = clean_env() | {
        "TELEGRAM_BOT_TOKEN": "local-token",
        "TELEGRAM_TEST_CHAT_ID": "test-only-chat",
        "TELEGRAM_CHAT_ID": "",
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check-config"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "mode": "config-check",
        "ready": True,
        "external_messages": 0,
    }


def test_local_env_file_loads_without_exports(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=file-token\nTELEGRAM_TEST_CHAT_ID=file-test-chat\n")
    result = check_config(env_file, clean_env())
    assert result.returncode == 0
    assert json.loads(result.stdout)["ready"] is True
    assert "file-token" not in result.stdout + result.stderr


def test_process_environment_overrides_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=file-token\n"
        "TELEGRAM_TEST_CHAT_ID=file-chat\n"
        "TELEGRAM_CHAT_ID=file-chat\n"
    )
    env = clean_env() | {
        "TELEGRAM_BOT_TOKEN": "env-token",
        "TELEGRAM_TEST_CHAT_ID": "env-test-chat",
    }
    result = check_config(env_file, env)
    assert result.returncode == 0
    assert json.loads(result.stdout)["ready"] is True
    env["TELEGRAM_BOT_TOKEN"] = ""
    missing = check_config(env_file, env)
    assert missing.returncode != 0  # An explicit empty environment value also overrides .env.
    assert "file-token" not in missing.stdout + missing.stderr


def test_missing_credentials_fail_without_network(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    result = check_config(env_file, clean_env())
    assert result.returncode != 0
    assert "token and separate test chat" in result.stderr
