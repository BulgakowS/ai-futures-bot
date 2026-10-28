"""Offline bounds for the explicit GKG pilot; no HTTP or active database."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from ai_futures import gdelt_gkg_pilot as pilot
from ai_futures.gdelt_gkg_forward import MAX_ARCHIVE, MAX_MANIFEST, PollResult


class FakeClock:
    def __init__(self):
        self.time = datetime(2026, 10, 19, tzinfo=UTC)
        self.waits = []
        self.on_wait = None

    def now(self):
        return self.time

    def monotonic(self):
        return (self.time - datetime(2026, 10, 19, tzinfo=UTC)).total_seconds()

    async def wait(self, seconds, stop):
        self.waits.append(seconds)
        self.time += timedelta(seconds=seconds)
        if self.on_wait:
            self.on_wait(stop)


class FakePoller:
    def __init__(self, clock, size=100, error=None, manifest_size=0):
        self.clock = clock
        self.size = size
        self.error = error
        self.manifest_size = manifest_size
        self.calls = []

    async def poll_once(self, *, download_budget_bytes):
        self.calls.append((self.clock.now(), download_budget_bytes))
        if self.error:
            return PollResult("error", "scan", error_code=self.error)
        return PollResult(
            "ok",
            "scan",
            archive_bytes=min(self.size, download_budget_bytes - self.manifest_size),
            manifest_bytes=self.manifest_size,
        )


@pytest.fixture(autouse=True)
def fake_counts(monkeypatch):
    async def counts(_database):
        return {"scans": 0, "slots_unknown": 0, "archive_bytes": 0}

    monkeypatch.setattr(pilot, "journal_counts", counts)

    async def latest(_database):
        return None

    monkeypatch.setattr(pilot, "latest_scan_at", latest, raising=False)


async def execute(clock, poller, stop=None):
    lines = []
    summary = await pilot.run_pilot(
        poller, object(), clock=clock, stop=stop or asyncio.Event(), emit=lines.append
    )
    return summary, lines


async def test_attempt_and_pacing_caps():
    clock = FakeClock()
    poller = FakePoller(clock)
    summary, lines = await execute(clock, poller)
    assert summary["attempts"] <= pilot.MAX_ATTEMPTS
    assert summary["reason"] in {"deadline", "attempt_cap"}
    assert len(lines) == summary["attempts"] + 1
    assert all(
        second[0] - first[0] >= pilot.POLL_INTERVAL
        for first, second in zip(poller.calls, poller.calls[1:], strict=False)
    )
    assert all(at < poller.calls[0][0] + pilot.DURATION for at, _ in poller.calls)


async def test_cumulative_archive_cap():
    clock = FakeClock()
    poller = FakePoller(clock, size=MAX_ARCHIVE)
    summary, _ = await execute(clock, poller)
    assert summary["reason"] == "byte_cap"
    assert summary["attempts"] == 16
    assert sum(min(MAX_ARCHIVE, budget) for _, budget in poller.calls) == pilot.MAX_TOTAL_ARCHIVE
    assert all(budget <= MAX_ARCHIVE + MAX_MANIFEST for _, budget in poller.calls)


async def test_cumulative_cap_includes_manifest_bytes():
    clock = FakeClock()
    poller = FakePoller(clock, size=MAX_ARCHIVE, manifest_size=MAX_MANIFEST)
    summary, _ = await execute(clock, poller)
    assert summary["reason"] == "byte_cap"
    assert summary["attempts"] == 16
    transferred = sum(
        min(MAX_ARCHIVE, budget - MAX_MANIFEST) + MAX_MANIFEST for _, budget in poller.calls
    )
    assert transferred == pilot.MAX_TOTAL_ARCHIVE


async def test_db_attempted_at_microsecond_skew_keeps_normal_progress(monkeypatch):
    clock = FakeClock()

    class SkewedPoller(FakePoller):
        def __init__(self):
            super().__init__(clock)
            self.persisted_at = None

        async def poll_once(self, *, download_budget_bytes):
            now = clock.now()
            if self.persisted_at and now - self.persisted_at < pilot.POLL_INTERVAL:
                self.calls.append((now, download_budget_bytes))
                return PollResult("throttled", None)
            result = await super().poll_once(download_budget_bytes=download_budget_bytes)
            self.persisted_at = now + timedelta(microseconds=200_000)
            return result

    poller = SkewedPoller()

    async def latest(_database):
        return poller.persisted_at

    monkeypatch.setattr(pilot, "latest_scan_at", latest)
    summary, lines = await execute(clock, poller)
    assert summary["attempts"] >= 3
    assert summary["reason"] in {"deadline", "attempt_cap"}
    assert all(line.get("status") != "throttled" for line in lines)
    assert all(
        second[0] - first[0] >= pilot.POLL_INTERVAL + timedelta(microseconds=200_000)
        for first, second in zip(poller.calls, poller.calls[1:], strict=False)
    )


async def test_frozen_wall_clock_stops_at_monotonic_deadline(monkeypatch):
    class FrozenWallClock(FakeClock):
        def __init__(self):
            super().__init__()
            self.elapsed = 0.0

        def monotonic(self):
            return self.elapsed

        async def wait(self, seconds, stop):
            self.elapsed += seconds

    clock = FrozenWallClock()
    poller = FakePoller(clock)

    async def latest(_database):
        return clock.now() if poller.calls else None

    monkeypatch.setattr(pilot, "latest_scan_at", latest)
    summary, _ = await execute(clock, poller)
    assert summary["reason"] == "deadline"
    assert summary["attempts"] == len(poller.calls) == 1
    assert clock.monotonic() == pilot.DURATION.total_seconds()


async def test_interrupt_preserves_completed_attempt():
    clock = FakeClock()
    clock.on_wait = lambda stop: stop.set()
    poller = FakePoller(clock)
    summary, lines = await execute(clock, poller)
    assert summary["reason"] == "interrupted"
    assert summary["attempts"] == 1
    assert lines[0]["attempt"] == 1


@pytest.mark.parametrize("error", ["manifest_http_429", "archive_http_429", "transport"])
async def test_any_poll_error_stops_without_retry(error):
    clock = FakeClock()
    poller = FakePoller(clock, error=error)
    summary, lines = await execute(clock, poller)
    assert summary["reason"] == "poll_error"
    assert summary["attempts"] == len(poller.calls) == 1
    assert lines[0]["error_code"] == error


def test_cli_default_off_and_disposable_database_only(monkeypatch):
    from scripts import pilot_gkg_forward as cli

    monkeypatch.delenv("GKG_PILOT_ENABLED", raising=False)
    assert not cli.enabled()
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("GKG_PILOT_ENABLED", "true")
    monkeypatch.setenv("GKG_FORWARD_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://active.example/live")
    seen = {}

    def fake_run(_args, *, cwd, env, capture_output, check, timeout):
        seen.update(env)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    path, settings = cli.create_disposable_database()
    try:
        assert cli.enabled()
        assert path.parent == __import__("pathlib").Path(__import__("tempfile").gettempdir())
        assert settings.database_url.get_secret_value() == seen["DATABASE_URL"]
        assert seen["DATABASE_URL"].startswith("sqlite+aiosqlite:////")
        assert "active.example" not in seen["DATABASE_URL"]
        assert "GKG_PILOT_ENABLED" not in seen
        assert settings.app_env == "test" and settings.app_mode == "paper"
    finally:
        path.unlink()
