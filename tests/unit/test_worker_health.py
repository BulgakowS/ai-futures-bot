"""A live heartbeat process must not conceal failed or stuck worker loops."""

from datetime import UTC, datetime, timedelta

from ai_futures.worker_health import components_healthy, ingestion_healthy


class Task:
    def __init__(self, finished=False):
        self.finished = finished

    def done(self):
        return self.finished


def test_worker_status_requires_progress_and_running_tasks():
    tasks = [Task(), Task(), Task()]
    assert not components_healthy(tasks, {"paper": None, "bot": None}, 100, 20)
    assert components_healthy(tasks, {"paper": 99, "bot": 90}, 100, 20)
    assert not components_healthy(tasks, {"paper": 79, "bot": 90}, 100, 20)
    tasks[1].finished = True
    assert not components_healthy(tasks, {"paper": 99, "bot": 99}, 100, 20)


def test_ingestion_status_requires_recent_ticker_progress():
    now = datetime(2026, 10, 16, tzinfo=UTC)
    assert not ingestion_healthy(None, now, 4)
    assert ingestion_healthy(now - timedelta(seconds=3), now, 4)
    assert not ingestion_healthy(now - timedelta(seconds=4), now, 4)
    assert not ingestion_healthy(now + timedelta(seconds=1), now, 4)
